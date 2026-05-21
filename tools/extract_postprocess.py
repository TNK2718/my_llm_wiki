"""LLM 抽出結果の後処理: source grounding + dedup + relation cascade。

`extract_graph_from_chunks` の戻り値に対して呼ぶ。
- ground: canonical_name (or mention_surface, or suffix-strip canonical) が
  source 本文の正規化済み substring に出現する entity のみ残す。 hallucination
  (token slip 等) を弾く。
- dedup: 同一 (normalize(canonical_name), proposed_type) を 1 件にまとめ、
  attributes は非空値を優先してマージする。
- relation cascade: 上記で落ちた entity を端点とする relation も落とす。
  compliance だけは to が free-text standard 名なので from のみ検証する。
- weak_relations は触らない (low-conf, gold で厳密採点しない)。
"""
from __future__ import annotations

import unicodedata

from extract_schema import (
    EntityExtraction,
    GraphExtraction,
    RelationExtraction,
)
from normalize import normalize

_CONTRACT_SUFFIXES = ("個別契約書", "契約書", "別表", "補遺", "書", "版")


def _ground_norm(s: str) -> str:
    """grounding 用の軽量 normalize。NFKC + lowercase のみ。

    `normalize.normalize` は空白・句読点を全て除去するため、本文中の
    "Bob in" のような単語境界が "bobin" に潰れて偽陽性 substring を生む。
    grounding では単語境界を保持する。
    """
    return unicodedata.normalize("NFKC", s or "").lower()


def _suffix_stripped(name: str) -> str | None:
    """末尾の契約・文書系 suffix を 1 つ落として返す。何も落とせなければ None。"""
    for suf in _CONTRACT_SUFFIXES:
        if name.endswith(suf) and len(name) > len(suf):
            return name[: -len(suf)]
    return None


def _is_grounded(e: EntityExtraction, norm_body: str) -> bool:
    candidates: list[str] = []
    if e.canonical_name:
        candidates.append(e.canonical_name)
        stripped = _suffix_stripped(e.canonical_name)
        if stripped:
            candidates.append(stripped)
    if e.mention_surface:
        candidates.append(e.mention_surface)
    for c in candidates:
        nc = _ground_norm(c)
        if nc and nc in norm_body:
            return True
    return False


def _merge_attributes(dst: dict, src: dict) -> dict:
    """非空値を優先してマージ。dst の非空値は保持、空のセルだけ src で埋める。"""
    out = dict(dst or {})
    for k, v in (src or {}).items():
        if v is None or v == "":
            continue
        if k not in out or out[k] is None or out[k] == "":
            out[k] = v
    return out


def _dedup_entities(entities: list[EntityExtraction]) -> list[EntityExtraction]:
    seen: dict[tuple[str, str], int] = {}
    out: list[EntityExtraction] = []
    for e in entities:
        if not e.canonical_name or not e.proposed_type:
            continue
        key = (normalize(e.canonical_name), e.proposed_type)
        if key in seen:
            idx = seen[key]
            merged = out[idx]
            merged.attributes = _merge_attributes(merged.attributes, e.attributes)
            if not merged.mention_surface and e.mention_surface:
                merged.mention_surface = e.mention_surface
            continue
        seen[key] = len(out)
        out.append(e)
    return out


def _endpoint_grounded(name: str, norm_body: str) -> bool:
    """relation 端点が source に substring として登場するか。

    entity dedup の結果に依存しない。LLM が relation だけで言及した参照
    (例: governance の to=contract が entities 一覧に無いケース) を不当に
    落とさないため。
    """
    if not name:
        return False
    n = _ground_norm(name)
    if not n:
        return False
    return n in norm_body


def _keep_relation(r: RelationExtraction, norm_body: str) -> bool:
    if not r.from_ or not r.to:
        return False
    if not _endpoint_grounded(r.from_, norm_body):
        return False
    if r.proposed_junction == "compliance":
        # to は free-text standard 名 (entity ではない) なので grounding 不要
        return True
    return _endpoint_grounded(r.to, norm_body)


def ground_and_dedup(g: GraphExtraction, source_body: str) -> GraphExtraction:
    norm_body = _ground_norm(source_body or "")

    grounded = [e for e in g.entities if _is_grounded(e, norm_body)]
    deduped = _dedup_entities(grounded)

    kept_relations = [r for r in g.relations if _keep_relation(r, norm_body)]

    return GraphExtraction(
        entities=deduped,
        relations=kept_relations,
        weak_relations=g.weak_relations,
    )
