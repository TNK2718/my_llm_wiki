"""candidate_entities_by_similarity / _across_tables の embedding 拡張テスト。

設計柱 D1 (norm_key + embedding cos sim) のうち embedding 部分を担保する。
Ollama は呼ばず fake_embed_cached fixture で偽 vector を返す。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import db as kg  # noqa: E402
import llm  # noqa: E402


def test_embedding_picks_up_variant_pair(tmp_db, fake_embed_cached):
    """『Anthropic』と『Anthropic, PBC』のような suffix 差を embedding で拾う。"""
    fake_embed_cached({
        "Anthropic":      [1.0, 0.0],
        "Anthropic, PBC": [0.95, 0.05],  # cos ≈ 0.997
    })
    with kg.writer(tmp_db):
        eid, _ = kg.upsert_entity(tmp_db, "organization", "Anthropic")

    q_vec = llm.embed_cached("Anthropic, PBC")
    cands = kg.candidate_entities_by_similarity(
        tmp_db, "organization", "Anthropic, PBC", query_embedding=q_vec,
    )
    assert cands
    top = cands[0]
    assert top["id"] == eid
    assert top["sim_emb"] is not None
    assert top["sim_emb"] >= 0.95


def test_embedding_recovers_when_trigram_misses(tmp_db, fake_embed_cached):
    """trigram でゼロ近くまで落ちる『東京大学 ↔ 東大』を embedding で救済。"""
    fake_embed_cached({
        "東京大学": [1.0, 0.0],
        "東大":     [0.98, 0.02],
    })
    with kg.writer(tmp_db):
        eid, _ = kg.upsert_entity(tmp_db, "organization", "東京大学")

    q_vec = llm.embed_cached("東大")
    cands_no_embed = kg.candidate_entities_by_similarity(
        tmp_db, "organization", "東大",
    )
    cands_with_embed = kg.candidate_entities_by_similarity(
        tmp_db, "organization", "東大", query_embedding=q_vec,
    )
    # embedding OFF: trigram 低すぎで拾わない
    assert not any(c["id"] == eid for c in cands_no_embed)
    # embedding ON: cosine で拾う
    hit = [c for c in cands_with_embed if c["id"] == eid]
    assert hit
    assert hit[0]["sim_emb"] >= 0.95
    assert hit[0]["norm_key_match"] is False


def test_silent_degrade_when_embedding_unavailable(tmp_db, fake_embed_cached):
    """fake_embed_cached が None を返す (= Ollama down 相当) でも trigram で動く。"""
    fake_embed_cached({})  # 全部 None
    with kg.writer(tmp_db):
        eid = kg.upsert_entity(tmp_db, "person", "Alice Smith")[0]

    q_vec = llm.embed_cached("alice smith")  # None
    cands = kg.candidate_entities_by_similarity(
        tmp_db, "person", "alice  smith", query_embedding=q_vec,
    )
    assert cands
    assert cands[0]["id"] == eid
    assert cands[0]["norm_key_match"] is True
    assert cands[0]["similarity"] == 1.0
    assert cands[0]["sim_emb"] is None  # 縮退で None のまま


def test_norm_key_match_still_wins_even_with_embedding(tmp_db, fake_embed_cached):
    """norm_key 完全一致は embedding を上書きせず常に similarity=1.0。"""
    fake_embed_cached({
        "Alice": [1.0, 0.0],
        "alice": [0.5, 0.5],  # cos ≈ 0.707 (低めだが norm_key 一致が優先される)
    })
    with kg.writer(tmp_db):
        eid = kg.upsert_entity(tmp_db, "person", "Alice")[0]

    q_vec = llm.embed_cached("alice")
    cands = kg.candidate_entities_by_similarity(
        tmp_db, "person", "alice", query_embedding=q_vec,
    )
    assert cands
    assert cands[0]["id"] == eid
    assert cands[0]["norm_key_match"] is True
    assert cands[0]["similarity"] == 1.0


def test_across_tables_threads_embedding(tmp_db, fake_embed_cached):
    """candidate_entities_across_tables が query_embedding を子関数に渡す。"""
    fake_embed_cached({
        "OpenAI":  [1.0, 0.0],
        "Open AI": [0.96, 0.04],
    })
    with kg.writer(tmp_db):
        kg.upsert_entity(tmp_db, "organization", "OpenAI")

    q_vec = llm.embed_cached("Open AI")
    cands = kg.candidate_entities_across_tables(
        tmp_db, "Open AI", query_embedding=q_vec,
    )
    org_hits = [c for c in cands if c["table"] == "organization"]
    assert org_hits
    assert org_hits[0]["sim_emb"] is not None
    assert org_hits[0]["sim_emb"] >= 0.95


def test_existing_dict_keys_preserved(tmp_db, fake_embed_cached):
    """既存 callers が依存するキーは保持。"""
    fake_embed_cached({})
    with kg.writer(tmp_db):
        kg.upsert_entity(tmp_db, "person", "Alice")
    cands = kg.candidate_entities_by_similarity(tmp_db, "person", "Alice")
    assert cands
    keys = set(cands[0].keys())
    assert {"id", "canonical_name", "norm_key", "similarity", "norm_key_match"} <= keys
    # 新規追加キーも返ること
    assert "sim_tri" in keys
    assert "sim_emb" in keys


def test_feature_flag_disables_embedding(tmp_db, fake_embed_cached, monkeypatch):
    """ENTITY_EMBED_ENABLED = False で query_embedding を渡しても trigram のみ動作。"""
    import config

    fake_embed_cached({
        "東京大学": [1.0, 0.0],
        "東大":     [0.98, 0.02],
    })
    with kg.writer(tmp_db):
        eid, _ = kg.upsert_entity(tmp_db, "organization", "東京大学")
    monkeypatch.setattr(config, "ENTITY_EMBED_ENABLED", False)

    q_vec = llm.embed_cached("東大")
    cands = kg.candidate_entities_by_similarity(
        tmp_db, "organization", "東大", query_embedding=q_vec,
    )
    # flag OFF なので embedding は使われず trigram で拾えない
    assert not any(c["id"] == eid for c in cands)


def test_ingest_resolve_entity_uses_embedding(tmp_db, fake_embed_cached):
    """ingest._resolve_entity が q_vec を計算して non_starter staging で同一テーブル
    候補を拾うこと (across_tables 経由)。"""
    import ingest
    from extract_schema import EntityExtraction
    import json

    fake_embed_cached({
        "Anthropic":      [1.0, 0.0],
        "Anthropic, PBC": [0.95, 0.05],
    })
    with kg.writer(tmp_db):
        kg.upsert_entity(tmp_db, "organization", "Anthropic")
        doc = kg.upsert_document(tmp_db, "d", "d", None, "body")
        # 未知型として ingest → across_tables で organization の Anthropic を拾う
        e = EntityExtraction(
            proposed_type="legal_entity",  # non-starter
            canonical_name="Anthropic, PBC",
            confidence=0.8,
        )
        ingest._resolve_entity(tmp_db, e, doc, {})

    row = tmp_db.execute(
        "SELECT match_summary FROM staging_extractions WHERE status='pending'"
    ).fetchone()
    assert row is not None
    m = json.loads(row["match_summary"])
    assert m["reason"] == "non_starter_type"
    assert m["existing_matches"], "embedding 経由で既存 Anthropic が候補に挙がる"
    top = m["existing_matches"][0]
    assert top["table"] == "organization"
    assert top["canonical_name"] == "Anthropic"
    assert top["sim_emb"] is not None
    assert top["sim_emb"] >= 0.95
