"""extract_postprocess.ground_and_dedup の単体テスト。

source grounding (canonical / mention_surface / suffix-stripped),
dedup (同一 canonical+type の attribute マージ), relation cascade
(端点が落とされた relation を drop, compliance は from のみ) を担保する。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

from extract_postprocess import ground_and_dedup  # noqa: E402
from extract_schema import (  # noqa: E402
    EntityExtraction,
    GraphExtraction,
    RelationExtraction,
)


def _ent(t: str, name: str, **kw) -> EntityExtraction:
    return EntityExtraction(proposed_type=t, canonical_name=name, **kw)


def _rel(j: str, frm: str, to: str) -> RelationExtraction:
    return RelationExtraction.model_validate({
        "proposed_junction": j, "from": frm, "to": to,
    })


def test_drops_unsourced_entity():
    body = "Bobcoin はリソース単位の計測単位です。"
    g = GraphExtraction(entities=[
        _ent("product", "IBM Bob"),       # no in body, but...
        _ent("product", "Bobcoin"),       # in body
        _ent("product", "bobin"),         # token slip — must drop
    ])
    out = ground_and_dedup(g, body)
    names = {e.canonical_name for e in out.entities}
    assert "Bobcoin" in names
    assert "bobin" not in names
    # IBM Bob は本文に無いので落ちる
    assert "IBM Bob" not in names


def test_keeps_canonical_with_suffix_in_body():
    body = "クラウド・サービス契約書 (http://www.ibm.com/terms/csa) が適用される。"
    g = GraphExtraction(entities=[
        _ent("contract", "クラウド・サービス契約"),  # gold canonical (suffix なし)
    ])
    out = ground_and_dedup(g, body)
    assert [e.canonical_name for e in out.entities] == ["クラウド・サービス契約"]


def test_dedups_same_canonical_across_chunks():
    body = "IBM Bob Pro の説明。"
    g = GraphExtraction(entities=[
        _ent("product", "IBM Bob Pro", attributes={"billing_period": "monthly"}),
        _ent("product", "IBM Bob Pro", attributes={"included_quota_units": 40}),
        _ent("product", "IBM Bob Pro", attributes={"billing_period": ""}),  # 空は無視
    ])
    out = ground_and_dedup(g, body)
    assert len(out.entities) == 1
    e = out.entities[0]
    assert e.attributes["billing_period"] == "monthly"
    assert e.attributes["included_quota_units"] == 40


def test_relation_cascade_drop():
    body = "IBM Bob は IBM 製。"
    g = GraphExtraction(
        entities=[
            _ent("product", "IBM Bob"),
            _ent("organization", "IBM"),
            # "bobin" は本文に無いので落ちる → 連鎖で manufacturing(bobin, IBM) も落ちる
        ],
        relations=[
            _rel("manufacturing", "IBM Bob", "IBM"),    # keep
            _rel("manufacturing", "bobin", "IBM"),      # drop (from 落ち)
        ],
    )
    out = ground_and_dedup(g, body)
    kept = [(r.proposed_junction, r.from_, r.to) for r in out.relations]
    assert ("manufacturing", "IBM Bob", "IBM") in kept
    assert ("manufacturing", "bobin", "IBM") not in kept


def test_compliance_only_requires_from():
    body = "本サービス記述書 i127-9285 は ISO 27001 に準拠。"
    g = GraphExtraction(
        entities=[_ent("contract", "i127-9285")],  # ISO 27001 は entity ではない
        relations=[
            _rel("compliance", "i127-9285", "ISO 27001"),   # keep (to は free-text)
            _rel("compliance", "missing-doc", "ISO 27001"), # drop (from 不在)
        ],
    )
    out = ground_and_dedup(g, body)
    kept = [(r.proposed_junction, r.from_, r.to) for r in out.relations]
    assert ("compliance", "i127-9285", "ISO 27001") in kept
    assert ("compliance", "missing-doc", "ISO 27001") not in kept


def test_weak_relations_passthrough():
    body = "IBM Bob は AI ツール。"
    g = GraphExtraction(
        entities=[_ent("product", "IBM Bob")],
        weak_relations=[
            {"subject": "IBM Bob", "predicate": "イネーブリング・ソフトウェア",
             "object": "Bob-Shell", "confidence": 0.4},
        ],
    )
    out = ground_and_dedup(g, body)
    assert len(out.weak_relations) == 1
    assert out.weak_relations[0].object == "Bob-Shell"
