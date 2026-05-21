"""Phase A: ingest が staging に書き込む match_summary の構造を検証する。

設計柱 D1 (LLM 提案は必ず既存テーブルとの類似度 + 新設提案を両出し) の決定論的算出
パートを担保する。LLM は呼ばない (ingest._resolve_entity を直接叩く)。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import db as kg  # noqa: E402
import ingest  # noqa: E402
from extract_schema import (  # noqa: E402
    EntityExtraction,
    ExistingMatchHint,
    RelationExtraction,
)


def _doc(conn, slug="doc1") -> int:
    return kg.upsert_document(conn, slug, slug, None, "body")


def _seed_person(conn, name="Alice Smith") -> int:
    eid, _ = kg.upsert_entity(conn, "person", name)
    return eid


# ---------- helpers under test ----------

def test_candidate_entities_norm_key_match_first(tmp_db):
    with kg.writer(tmp_db):
        eid = _seed_person(tmp_db, "Alice Smith")
        kg.upsert_entity(tmp_db, "person", "Bob")
    cands = kg.candidate_entities_by_similarity(tmp_db, "person", "alice  smith")
    assert cands, "should return at least the norm_key match"
    assert cands[0]["id"] == eid
    assert cands[0]["norm_key_match"] is True
    assert cands[0]["similarity"] == 1.0


def test_candidate_entities_filters_by_min_sim(tmp_db):
    with kg.writer(tmp_db):
        _seed_person(tmp_db, "Alice")
    cands = kg.candidate_entities_by_similarity(
        tmp_db, "person", "Zachariah", min_sim=0.9,
    )
    assert cands == []


def test_candidate_entities_across_tables(tmp_db):
    with kg.writer(tmp_db):
        _seed_person(tmp_db, "Alpha")
        kg.upsert_entity(tmp_db, "organization", "Alpha")
    cands = kg.candidate_entities_across_tables(tmp_db, "Alpha")
    tables = {c["table"] for c in cands}
    assert {"person", "organization"} <= tables


# ---------- ingest match_summary 構造 ----------

def test_non_starter_type_emits_existing_matches(tmp_db):
    with kg.writer(tmp_db):
        _seed_person(tmp_db, "Acme Founder")
        doc = _doc(tmp_db)
        e = EntityExtraction(
            proposed_type="inventor",
            canonical_name="Acme Founder",
            confidence=0.8,
        )
        ingest._resolve_entity(tmp_db, e, doc, {})

    row = tmp_db.execute(
        "SELECT match_summary FROM staging_extractions WHERE status='pending'"
    ).fetchone()
    assert row is not None
    m = json.loads(row["match_summary"])
    assert m["reason"] == "non_starter_type"
    assert "starter_types" in m
    assert m["existing_matches"], "決定論的に既存マッチが出ているはず"
    top = m["existing_matches"][0]
    assert top["table"] == "person"
    assert top["norm_key_match"] is True
    assert top["canonical_name"] == "Acme Founder"


def test_non_starter_type_propagates_llm_hints(tmp_db):
    with kg.writer(tmp_db):
        doc = _doc(tmp_db)
        e = EntityExtraction(
            proposed_type="algorithm",
            canonical_name="FFT",
            confidence=0.8,
            existing_matches=[
                ExistingMatchHint(table="product", name="FFTLib", rationale="略称的に近い"),
            ],
        )
        ingest._resolve_entity(tmp_db, e, doc, {})

    row = tmp_db.execute(
        "SELECT match_summary FROM staging_extractions WHERE status='pending'"
    ).fetchone()
    m = json.loads(row["match_summary"])
    assert m["llm_hints"], "LLM 由来の existing_matches は llm_hints として保持される"
    assert m["llm_hints"][0]["name"] == "FFTLib"
    assert m["llm_hints"][0]["table"] == "product"


def test_unknown_column_match_summary_structure(tmp_db):
    with kg.writer(tmp_db):
        doc = _doc(tmp_db)
        e = EntityExtraction(
            proposed_type="person",
            canonical_name="Bob",
            confidence=0.9,
            attributes={"favorite_color": "blue"},  # unknown column
        )
        ingest._resolve_entity(tmp_db, e, doc, {})

    rows = tmp_db.execute(
        "SELECT match_summary FROM staging_extractions WHERE status='pending'"
    ).fetchall()
    assert any(json.loads(r["match_summary"]).get("reason") == "unknown_column" for r in rows)
    unknown = [json.loads(r["match_summary"]) for r in rows
               if json.loads(r["match_summary"]).get("reason") == "unknown_column"][0]
    assert unknown["column_name"] == "favorite_color"
    assert "known_columns" in unknown


def test_low_confidence_attribute_includes_same_type_candidates(tmp_db):
    with kg.writer(tmp_db):
        _seed_person(tmp_db, "Alice")
        _seed_person(tmp_db, "Alicia")
        doc = _doc(tmp_db)
        e = EntityExtraction(
            proposed_type="person",
            canonical_name="Alice",
            confidence=0.2,  # < LOW_CONFIDENCE_THRESHOLD (0.3)
            attributes={"birth_date": "1990-01-01"},
        )
        ingest._resolve_entity(tmp_db, e, doc, {})

    rows = tmp_db.execute(
        "SELECT match_summary FROM staging_extractions WHERE status='pending'"
    ).fetchall()
    lowconf = [json.loads(r["match_summary"]) for r in rows
               if json.loads(r["match_summary"]).get("reason") == "low_confidence"]
    assert lowconf, "低 conf 属性 claim は staging に積まれる"
    m = lowconf[0]
    assert m["column_name"] == "birth_date"
    assert m["existing_matches"], "同型内の候補が伝搬している"
    # Alice 自身が含まれる (norm_key 完全一致で先頭)
    assert m["existing_matches"][0]["canonical_name"] == "Alice"


def test_cardinality_violation_carries_structured_reason(tmp_db):
    with kg.writer(tmp_db):
        # 既存 manufacturing 行を作る
        prod, _ = kg.upsert_entity(tmp_db, "product", "SmartScan")
        org_a, _ = kg.upsert_entity(tmp_db, "organization", "Acme")
        org_b, _ = kg.upsert_entity(tmp_db, "organization", "Beta")
        doc = _doc(tmp_db)
        kg.find_or_create_manufacturing(tmp_db, prod, org_a, doc, confidence=0.8)
        # 別 org_b を主張 → cardinality violation
        r = RelationExtraction(
            proposed_junction="manufacturing",
            **{"from": "SmartScan", "to": "Beta"},
            attributes={},
            confidence=0.8,
            evidence="another doc says Beta makes it",
        )
        name_to_id = {("product", "SmartScan"): prod, ("organization", "Beta"): org_b}
        ingest._resolve_relation(tmp_db, r, doc, name_to_id, {})

    rows = tmp_db.execute(
        "SELECT match_summary FROM staging_extractions WHERE status='pending'"
    ).fetchall()
    card = [json.loads(r["match_summary"]) for r in rows
            if json.loads(r["match_summary"]).get("reason") == "cardinality_violation"]
    assert card, "cardinality violation は staging に積まれる"
    m = card[0]
    assert m["rule"] == "UNIQUE(product_id)"
    assert m["junction"] == "manufacturing"


# ---------- in-doc dedup (Pre-DB confidence-based selection) ----------

def _pv_relation(from_: str, to: str, conf: float) -> RelationExtraction:
    return RelationExtraction(
        proposed_junction="product_variant",
        **{"from": from_, "to": to},
        attributes={},
        confidence=conf,
        evidence="dummy",
    )


def test_indoc_dedup_picks_highest_confidence_winner(tmp_db):
    """i127-9285 リプロ: 逆向き (Pro→Bob, 0.9) が direction conflict で loser に。"""
    with kg.writer(tmp_db):
        bob,   _ = kg.upsert_entity(tmp_db, "product", "IBM Bob")
        pro,   _ = kg.upsert_entity(tmp_db, "product", "IBM Bob Pro")
        plus,  _ = kg.upsert_entity(tmp_db, "product", "IBM Bob Pro Plus")
        ultra, _ = kg.upsert_entity(tmp_db, "product", "IBM Bob Ultra")
    name_to_id = {
        ("product", "IBM Bob"):          bob,
        ("product", "IBM Bob Pro"):      pro,
        ("product", "IBM Bob Pro Plus"): plus,
        ("product", "IBM Bob Ultra"):    ultra,
    }
    r0 = _pv_relation("IBM Bob Pro", "IBM Bob",          0.9)
    r1 = _pv_relation("IBM Bob",     "IBM Bob Pro",      0.7)
    r2 = _pv_relation("IBM Bob",     "IBM Bob Pro Plus", 0.7)
    r3 = _pv_relation("IBM Bob",     "IBM Bob Ultra",    0.7)
    relations = [r0, r1, r2, r3]

    gb_winners, gb_losers = ingest._dedup_cardinality_relations(relations, name_to_id)
    # variant_id が全部別なので groupby では落ちない
    assert len(gb_winners) == 4
    assert gb_losers == []

    pv_winners, pv_losers = ingest._resolve_product_variant_direction(
        gb_winners, name_to_id,
    )
    # 逆向き r0 が loser、正しい向き 3 件が残る
    assert len(pv_winners) == 3
    assert all(w.from_ == "IBM Bob" for w in pv_winners)
    assert {w.to for w in pv_winners} == {"IBM Bob Pro", "IBM Bob Pro Plus", "IBM Bob Ultra"}
    assert len(pv_losers) == 1
    loser, chosen_winner = pv_losers[0]
    assert loser.from_ == "IBM Bob Pro" and loser.to == "IBM Bob"
    assert chosen_winner in pv_winners


def test_indoc_dedup_tiebreaker_uses_first_index(tmp_db):
    """同 cardinality key + 同 conf なら先頭が winner。"""
    with kg.writer(tmp_db):
        bob, _ = kg.upsert_entity(tmp_db, "product", "Bob")
        pro, _ = kg.upsert_entity(tmp_db, "product", "Pro")
    name_to_id = {("product", "Bob"): bob, ("product", "Pro"): pro}
    # 同じ variant=Pro を target にする 2 件、同 conf
    r0 = _pv_relation("Bob", "Pro", 0.7)
    r1 = _pv_relation("Bob", "Pro", 0.7)  # 内容同じだが別 instance
    winners, losers = ingest._dedup_cardinality_relations([r0, r1], name_to_id)
    assert len(winners) == 1
    assert winners[0] is r0
    assert len(losers) == 1
    assert losers[0][0] is r1
    assert losers[0][1] is r0


def test_indoc_dedup_stages_loser_as_alternative_proposal(tmp_db):
    """loser が staging に reason='alternative_proposal_lower_confidence' で記録される。"""
    with kg.writer(tmp_db):
        doc = _doc(tmp_db)
        loser  = _pv_relation("IBM Bob Pro", "IBM Bob",     0.9)
        winner = _pv_relation("IBM Bob",     "IBM Bob Pro", 0.7)
        ingest._stage_alternative_proposal(tmp_db, doc, loser, winner, {})

    row = tmp_db.execute(
        "SELECT proposed_table, match_summary FROM staging_extractions"
        " WHERE status='pending'"
    ).fetchone()
    assert row is not None
    assert row["proposed_table"] == "product_variant"
    m = json.loads(row["match_summary"])
    assert m["reason"] == "alternative_proposal_lower_confidence"
    assert m["junction"] == "product_variant"
    assert m["loser_from"] == "IBM Bob Pro"
    assert m["loser_to"]   == "IBM Bob"
    assert m["winner_from"] == "IBM Bob"
    assert m["winner_to"]   == "IBM Bob Pro"
    assert m["loser_confidence"]  == 0.9
    assert m["winner_confidence"] == 0.7


def test_indoc_dedup_handles_compliance_keyed_by_standard(tmp_db):
    """compliance も groupby 対象。同 (contract, standard) で conf 高い方が winner。"""
    with kg.writer(tmp_db):
        ct, _ = kg.upsert_entity(tmp_db, "contract", "MSA")
    name_to_id = {("contract", "MSA"): ct}
    r_high = RelationExtraction(
        proposed_junction="compliance",
        **{"from": "MSA", "to": "ISO 27001"},
        attributes={},
        confidence=0.9,
        evidence="src high",
    )
    r_low = RelationExtraction(
        proposed_junction="compliance",
        **{"from": "MSA", "to": "ISO 27001"},
        attributes={},
        confidence=0.7,
        evidence="src low",
    )
    winners, losers = ingest._dedup_cardinality_relations([r_low, r_high], name_to_id)
    assert len(winners) == 1
    assert winners[0] is r_high
    assert len(losers) == 1
    assert losers[0][0] is r_low
    assert losers[0][1] is r_high


def test_every_match_summary_is_valid_json(tmp_db):
    """全 staging 経路で match_summary が JSON.parse 可能であること。"""
    with kg.writer(tmp_db):
        doc = _doc(tmp_db)
        # 1. 未知型
        ingest._resolve_entity(
            tmp_db,
            EntityExtraction(proposed_type="algo", canonical_name="X", confidence=0.9),
            doc, {},
        )
        # 2. 未知列
        ingest._resolve_entity(
            tmp_db,
            EntityExtraction(
                proposed_type="person", canonical_name="P",
                attributes={"hat_color": "red"}, confidence=0.9,
            ),
            doc, {},
        )
        # 3. 低 conf attribute
        ingest._resolve_entity(
            tmp_db,
            EntityExtraction(
                proposed_type="person", canonical_name="Q",
                attributes={"birth_date": "1990"}, confidence=0.2,
            ),
            doc, {},
        )
    rows = tmp_db.execute("SELECT match_summary FROM staging_extractions").fetchall()
    assert len(rows) >= 3
    for r in rows:
        m = json.loads(r["match_summary"])
        assert "reason" in m
