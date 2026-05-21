"""Typed-schema ingest パイプライン。docs/typed-schema-design.md「Ingest フロー」準拠。

  python tools/ingest.py raw/extracted/議事録.md

手順:
  1. semantic chunking (markdown header / 段落境界を尊重)
  2. typed extract (entities/relations/weak_relations) — SLM, per-chunk + running entity hint
  3. resolve_or_stage: 決定論的類似度（norm_key + similarity）で既存マッチ判定
     - starter 型 + 高 conf  → upsert + record_claim (§3 状態遷移)
     - 未知型 / new_table_proposal → staging_extractions + schema_proposal
     - 低 conf attribute       → staging_extractions
  4. resolve_relation:
     - starter junction + 高 conf → find_or_create_<junction> + record_existence_claim
     - cardinality 違反         → staging_extractions
     - 未知 junction / 低 conf → weak_relations
     - N≥5 同 predicate         → schema_proposal 自動起票
  5. log 追記
"""
from __future__ import annotations

import json
import re
import sys
import textwrap
from typing import Iterable

import config
import db as kg
import llm
import logadd
from extract_schema import (
    EntityExtraction,
    GraphExtraction,
    RelationExtraction,
    WeakRelationExtraction,
    parse_extraction,
)


# ---------- chunking (旧実装を流用) ----------
def _char_split(text: str, size: int) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)] or [""]


def _split_by_header_level(text: str, level: int) -> list[str]:
    pat = re.compile(rf"(?m)^#{{{level}}}\s")
    starts = [m.start() for m in pat.finditer(text)]
    if not starts:
        return [text]
    sections = []
    if starts[0] != 0:
        sections.append(text[: starts[0]])
    for i, s in enumerate(starts):
        e = starts[i + 1] if i + 1 < len(starts) else len(text)
        sections.append(text[s:e])
    return [s for s in sections if s.strip()]


def _split_recursive(text: str, max_chars: int, levels: list[int]) -> list[str]:
    if len(text) <= max_chars or not levels:
        return [text]
    level = levels[0]
    parts = _split_by_header_level(text, level)
    if len(parts) == 1:
        return _split_recursive(text, max_chars, levels[1:])
    out = []
    for p in parts:
        if len(p) <= max_chars:
            out.append(p)
        else:
            out.extend(_split_recursive(p, max_chars, levels[1:]))
    return out


def _greedy_pack(units: list[str], max_chars: int, sep: str = "\n\n") -> list[str]:
    out, cur, cur_len = [], [], 0
    for u in units:
        ul = len(u)
        if not cur:
            cur, cur_len = [u], ul
            continue
        if cur_len + len(sep) + ul > max_chars:
            out.append(sep.join(cur))
            cur, cur_len = [u], ul
        else:
            cur.append(u)
            cur_len += len(sep) + ul
    if cur:
        out.append(sep.join(cur))
    return out


def semantic_chunk(text: str, max_chars: int) -> list[str]:
    if not text:
        return [""]
    if len(text) <= max_chars:
        return [text]
    if re.search(r"(?m)^#{1,6}\s", text):
        units = _split_recursive(text, max_chars, [2, 3, 4, 5, 6])
    else:
        units = [u for u in re.split(r"\n\s*\n", text) if u.strip()] or [text]
    expanded = []
    for u in units:
        if len(u) <= max_chars:
            expanded.append(u)
            continue
        paras = [p for p in re.split(r"\n\s*\n", u) if p.strip()]
        for p in paras:
            if len(p) <= max_chars:
                expanded.append(p)
            else:
                expanded.extend(_char_split(p, max_chars))
    return _greedy_pack(expanded, max_chars)


# ---------- extract ----------
def load_prompt(name):
    return (config.PROMPTS / name).read_text(encoding="utf-8")


def _format_known_entities(known: list[dict]) -> str:
    if not known:
        return ""
    block = "\n".join(
        f"- {e['canonical_name']} ({e.get('proposed_type', '?')})" for e in known
    )
    return (
        "このドキュメント内で既に登場したエンティティ"
        "（同一対象を指す新しい言及があれば、同じ canonical_name を再利用してください）:\n"
        f"{block}\n\n"
    )


def extract_graph_from_chunks(
    chunks: list[str], source_body: str | None = None,
) -> GraphExtraction:
    from extract_postprocess import ground_and_dedup

    tmpl = load_prompt("extract_graph.txt")
    body = source_body if source_body is not None else "\n\n".join(chunks)
    if len(chunks) == 1:
        prompt = tmpl.replace("{KNOWN_ENTITIES}", "").replace("{CONTENT}", chunks[0])
        return ground_and_dedup(parse_extraction(llm.ask_json(prompt)), body)

    merged = GraphExtraction()
    seen_names: set[str] = set()
    seen_summary: list[dict] = []
    for c in chunks:
        prompt = tmpl.replace(
            "{KNOWN_ENTITIES}", _format_known_entities(seen_summary)
        ).replace("{CONTENT}", c)
        part = parse_extraction(llm.ask_json(prompt))
        for e in part.entities:
            if e.canonical_name and e.canonical_name not in seen_names:
                seen_summary.append({
                    "canonical_name": e.canonical_name,
                    "proposed_type": e.proposed_type,
                })
                seen_names.add(e.canonical_name)
        merged.entities.extend(part.entities)
        merged.relations.extend(part.relations)
        merged.weak_relations.extend(part.weak_relations)
    return ground_and_dedup(merged, body)


# ---------- resolve ----------
STARTER_ENTITY_TYPES = set(kg.ENTITY_TABLES)
STARTER_JUNCTIONS = set(kg.RELATION_CLAIM_COLUMNS)


def _llm_hints(e: EntityExtraction) -> list[dict]:
    return [
        {"table": h.table, "name": h.name, "rationale": h.rationale}
        for h in (e.existing_matches or [])
    ]


def _build_match_summary(
    reason: str,
    *,
    existing_matches: list[dict] | None = None,
    llm_hints: list[dict] | None = None,
    extra: dict | None = None,
) -> str:
    payload: dict = {"reason": reason}
    if existing_matches:
        payload["existing_matches"] = existing_matches
    if llm_hints:
        payload["llm_hints"] = llm_hints
    if extra:
        payload.update(extra)
    return json.dumps(payload, ensure_ascii=False)


def _resolve_entity(
    db,
    e: EntityExtraction,
    doc_id: int,
    counters: dict,
) -> int | None:
    """resolve_or_stage: 1 entity を typed table に流す。返り値: entity_id (流せた時) or None.

    starter 型以外の主張は staging + schema_proposal に積み、entity_id は返さない。
    """
    if not e.canonical_name:
        return None
    if e.proposed_type not in STARTER_ENTITY_TYPES:
        # 未知型 → staging + schema_proposal。decisive 類似度は pipeline 側で算出
        existing = kg.candidate_entities_across_tables(db, e.canonical_name)
        kg.add_staging_extraction(
            db, doc_id,
            raw_payload=e.model_dump_json(),
            proposed_table=e.proposed_type,
            match_summary=_build_match_summary(
                "non_starter_type",
                existing_matches=existing,
                llm_hints=_llm_hints(e),
                extra={"starter_types": list(STARTER_ENTITY_TYPES)},
            ),
        )
        if e.new_table_proposal:
            kg.add_schema_proposal(
                db,
                kind="new_table",
                target_table=e.new_table_proposal.table,
                proposed_ddl=_render_new_table_ddl(e.new_table_proposal),
                rationale=e.new_table_proposal.rationale,
                evidence_docs=json.dumps([doc_id]),
            )
        counters["staged_entity"] = counters.get("staged_entity", 0) + 1
        return None

    table = e.proposed_type
    eid, how = kg.upsert_entity(db, table, e.canonical_name)
    if e.mention_surface and e.mention_surface != e.canonical_name:
        kg.add_alias(db, table, eid, e.mention_surface)
        kg.add_mention(db, doc_id, table, eid, e.mention_surface)
    else:
        kg.add_mention(db, doc_id, table, eid, e.canonical_name)
    counters[f"entity_{how}"] = counters.get(f"entity_{how}", 0) + 1

    # attributes → record_claim
    columns = kg.ENTITY_CLAIM_COLUMNS[table]
    for col_name, value in (e.attributes or {}).items():
        if value is None or value == "":
            continue
        if col_name == "canonical_name":
            # canonical_name は upsert で既に set 済み。重複 claim はスキップ。
            continue
        if col_name not in columns:
            # 未知列 → staging + schema_proposal (new_column)
            kg.add_staging_extraction(
                db, doc_id,
                raw_payload=json.dumps(
                    {"entity_table": table, "entity_id": eid,
                     "column_name": col_name, "value": value,
                     "confidence": e.confidence}, ensure_ascii=False,
                ),
                proposed_table=table,
                match_summary=_build_match_summary(
                    "unknown_column",
                    extra={"known_columns": list(columns), "column_name": col_name},
                ),
            )
            kg.add_schema_proposal(
                db,
                kind="new_column",
                target_table=table,
                proposed_ddl=f"ALTER TABLE {table} ADD COLUMN {col_name} TEXT;",
                rationale=f"resolve_or_stage: unknown column {col_name!r} on {table}",
                evidence_docs=json.dumps([doc_id]),
            )
            counters["staged_attribute"] = counters.get("staged_attribute", 0) + 1
            continue
        r = kg.record_claim(
            db, table, eid, col_name, value, doc_id,
            evidence=e.evidence, confidence=e.confidence,
        )
        if r.result == kg.RecordClaimResult.REJECTED_LOW_CONFIDENCE:
            existing = kg.candidate_entities_by_similarity(db, table, e.canonical_name)
            kg.add_staging_extraction(
                db, doc_id,
                raw_payload=json.dumps(
                    {"kind": "attribute_claim", "entity_table": table,
                     "entity_id": eid, "column_name": col_name, "value": value,
                     "confidence": e.confidence}, ensure_ascii=False,
                ),
                proposed_table=table,
                match_summary=_build_match_summary(
                    "low_confidence",
                    existing_matches=existing,
                    extra={"column_name": col_name, "confidence": e.confidence},
                ),
            )
            counters["staged_lowconf_attr"] = counters.get("staged_lowconf_attr", 0) + 1
        else:
            counters[f"claim_{r.result.value}"] = counters.get(f"claim_{r.result.value}", 0) + 1
    return eid


_JUNCTION_FROM_TYPE = {
    "employment":      ("person", "organization"),
    "manufacturing":   ("product", "organization"),
    "org_hierarchy":   ("organization", "organization"),
    "product_variant": ("product", "product"),
    "governance":      ("product", "contract"),
    # compliance は (contract, standard_name) で 2nd エンドポイントが entity でないので
    # _JUNCTION_FROM_TYPE には載せず、_resolve_relation で個別ハンドルする
}


def _find_resolved(name_to_id: dict, table_hint: str, name: str) -> tuple[str, int] | None:
    """name_to_id は (table, name) → id だが、proposed_junction の from/to は型不明で来る。
    table_hint を優先し見つからなければ全 starter table を順に試す。"""
    if not name:
        return None
    for tbl in (table_hint,) + tuple(t for t in kg.ENTITY_TABLES if t != table_hint):
        eid = name_to_id.get((tbl, name))
        if eid is not None:
            return tbl, eid
    return None


def _resolve_relation(
    db,
    r: RelationExtraction,
    doc_id: int,
    name_to_id: dict,
    counters: dict,
) -> None:
    """starter junction or weak_relations / staging に振り分け。"""
    junction = r.proposed_junction
    if junction not in STARTER_JUNCTIONS:
        # 未知 junction → weak_relations 経由で受ける
        kg.add_weak_relation(
            db,
            subject_table="organization",  # plausible default; weak は object_text で扱う
            subject_id=0,
            predicate=junction,
            document_id=doc_id,
            object_text=f"{r.from_} -> {r.to}",
            evidence=r.evidence,
            confidence=min(r.confidence, 0.3),
        )
        counters["weak_unknown_junction"] = counters.get("weak_unknown_junction", 0) + 1
        return

    # compliance は (contract, standard_name TEXT) で右辺が entity ではない特例
    if junction == "compliance":
        contract_src = _find_resolved(name_to_id, "contract", r.from_)
        if contract_src is None or contract_src[0] != "contract":
            counters["compliance_unresolved_contract"] = counters.get("compliance_unresolved_contract", 0) + 1
            return
        standard = (r.to or "").strip()
        if not standard:
            return
        cert_until = (r.attributes or {}).get("certified_until")
        cid, how, _ = kg.find_or_create_compliance(
            db, contract_src[1], standard, doc_id,
            certified_until=cert_until,
            evidence=r.evidence, confidence=r.confidence,
        )
        counters[f"compliance_{how}"] = counters.get(f"compliance_{how}", 0) + 1
        kg.record_existence_claim(
            db, "compliance", cid, doc_id,
            evidence=r.evidence, confidence=r.confidence,
        )
        return

    expected_from, expected_to = _JUNCTION_FROM_TYPE[junction]
    src = _find_resolved(name_to_id, expected_from, r.from_)
    dst = _find_resolved(name_to_id, expected_to, r.to)
    if src is None or dst is None:
        counters["rel_unresolved_endpoint"] = counters.get("rel_unresolved_endpoint", 0) + 1
        return
    # 期待型に endpoints が解決されているか検査。LLM が誤型で結びつけた場合 (例 org_hierarchy
    # の from に person を渡された) は FK 違反になるので weak へ逃がす
    expected_pair = {expected_from, expected_to}
    actual_pair = {src[0], dst[0]}
    if actual_pair != expected_pair:
        kg.add_weak_relation(
            db,
            subject_table=src[0], subject_id=src[1],
            predicate=junction,
            object_table=dst[0], object_id=dst[1],
            document_id=doc_id,
            evidence=r.evidence,
            confidence=min(r.confidence, 0.3),
        )
        counters["weak_endpoint_type_mismatch"] = counters.get("weak_endpoint_type_mismatch", 0) + 1
        return

    if junction == "employment":
        person_id = src[1] if src[0] == "person" else dst[1]
        org_id = dst[1] if dst[0] == "organization" else src[1]
        start_date = (r.attributes or {}).get("start_date")
        eid, how = kg.find_or_create_employment(
            db, person_id, org_id, start_date, doc_id,
            evidence=r.evidence, confidence=r.confidence,
        )
        counters[f"employment_{how}"] = counters.get(f"employment_{how}", 0) + 1
        ex = kg.record_existence_claim(
            db, "employment", eid, doc_id,
            evidence=r.evidence, confidence=r.confidence,
        )
        if ex.result == kg.RecordClaimResult.REJECTED_LOW_CONFIDENCE:
            _route_weak(db, "person", person_id, "employment", "organization", org_id, doc_id, r)
            return
        for col in ("role", "end_date"):
            val = (r.attributes or {}).get(col)
            if val:
                kg.record_relation_claim(
                    db, "employment", eid, col, val, doc_id,
                    evidence=r.evidence, confidence=r.confidence,
                )

    elif junction == "manufacturing":
        prod_id = src[1] if src[0] == "product" else dst[1]
        org_id = dst[1] if dst[0] == "organization" else src[1]
        mid, how, conflict_with = kg.find_or_create_manufacturing(
            db, prod_id, org_id, doc_id,
            evidence=r.evidence, confidence=r.confidence,
        )
        counters[f"manufacturing_{how}"] = counters.get(f"manufacturing_{how}", 0) + 1
        if how == "cardinality_violation":
            kg.add_staging_extraction(
                db, doc_id,
                raw_payload=json.dumps(
                    {"kind": "cardinality_violation",
                     "junction": "manufacturing",
                     "product_id": prod_id,
                     "proposed_org_id": org_id,
                     "existing_manufacturing_id": conflict_with,
                     "confidence": r.confidence}, ensure_ascii=False,
                ),
                proposed_table="manufacturing",
                match_summary=_build_match_summary(
                    "cardinality_violation",
                    extra={
                        "rule": "UNIQUE(product_id)",
                        "junction": "manufacturing",
                        "existing_manufacturing_id": conflict_with,
                    },
                ),
            )
            return
        kg.record_existence_claim(
            db, "manufacturing", mid, doc_id,
            evidence=r.evidence, confidence=r.confidence,
        )

    elif junction == "governance":
        # from = product, to = contract
        product_id = src[1] if src[0] == "product" else dst[1]
        contract_id = dst[1] if dst[0] == "contract" else src[1]
        gid, how, _ = kg.find_or_create_governance(
            db, product_id, contract_id, doc_id,
            evidence=r.evidence, confidence=r.confidence,
        )
        counters[f"governance_{how}"] = counters.get(f"governance_{how}", 0) + 1
        kg.record_existence_claim(
            db, "governance", gid, doc_id,
            evidence=r.evidence, confidence=r.confidence,
        )

    elif junction == "product_variant":
        # from = parent product, to = variant product。自己参照は weak へ
        parent_id = src[1]
        variant_id = dst[1]
        if parent_id == variant_id:
            kg.add_weak_relation(
                db,
                subject_table="product", subject_id=parent_id,
                predicate="self_variant",
                object_table="product", object_id=variant_id,
                document_id=doc_id, evidence=r.evidence,
                confidence=min(r.confidence, 0.3),
            )
            counters["weak_self_variant"] = counters.get("weak_self_variant", 0) + 1
            return
        pvid, how, conflict_with = kg.find_or_create_product_variant(
            db, parent_id, variant_id, doc_id,
            evidence=r.evidence, confidence=r.confidence,
        )
        counters[f"product_variant_{how}"] = counters.get(f"product_variant_{how}", 0) + 1
        if how == "cardinality_violation":
            kg.add_staging_extraction(
                db, doc_id,
                raw_payload=json.dumps(
                    {"kind": "cardinality_violation",
                     "junction": "product_variant",
                     "variant_product_id": variant_id,
                     "proposed_parent_id": parent_id,
                     "existing_product_variant_id": conflict_with}, ensure_ascii=False,
                ),
                proposed_table="product_variant",
                match_summary=_build_match_summary(
                    "cardinality_violation",
                    extra={
                        "rule": "UNIQUE(variant_product_id)",
                        "junction": "product_variant",
                        "existing_product_variant_id": conflict_with,
                    },
                ),
            )
            return
        kg.record_existence_claim(
            db, "product_variant", pvid, doc_id,
            evidence=r.evidence, confidence=r.confidence,
        )

    elif junction == "org_hierarchy":
        # from = parent, to = child
        parent_id = src[1]
        child_id = dst[1]
        oid, how, conflict_with = kg.find_or_create_org_hierarchy(
            db, parent_id, child_id, doc_id,
            evidence=r.evidence, confidence=r.confidence,
        )
        counters[f"org_hierarchy_{how}"] = counters.get(f"org_hierarchy_{how}", 0) + 1
        if how == "cardinality_violation":
            kg.add_staging_extraction(
                db, doc_id,
                raw_payload=json.dumps(
                    {"kind": "cardinality_violation",
                     "junction": "org_hierarchy",
                     "child_org_id": child_id,
                     "proposed_parent_id": parent_id,
                     "existing_org_hierarchy_id": conflict_with}, ensure_ascii=False,
                ),
                proposed_table="org_hierarchy",
                match_summary=_build_match_summary(
                    "cardinality_violation",
                    extra={
                        "rule": "UNIQUE(child_org_id)",
                        "junction": "org_hierarchy",
                        "existing_org_hierarchy_id": conflict_with,
                    },
                ),
            )
            return
        kg.record_existence_claim(
            db, "org_hierarchy", oid, doc_id,
            evidence=r.evidence, confidence=r.confidence,
        )


def _route_weak(
    db,
    subj_table: str,
    subj_id: int,
    predicate: str,
    obj_table: str | None,
    obj_id: int | None,
    doc_id: int,
    r: RelationExtraction,
) -> None:
    kg.add_weak_relation(
        db,
        subject_table=subj_table,
        subject_id=subj_id,
        predicate=predicate,
        object_table=obj_table,
        object_id=obj_id,
        document_id=doc_id,
        evidence=r.evidence,
        confidence=min(r.confidence, 0.3),
    )


def _route_weak_extraction(db, w: WeakRelationExtraction, doc_id: int, name_to_id: dict) -> None:
    """LLM が直接 weak_relations に積むよう指示した主張を取り込む。"""
    subj = None
    for tbl in kg.ENTITY_TABLES:
        eid = name_to_id.get((tbl, w.subject))
        if eid is not None:
            subj = (tbl, eid)
            break
    if subj is None:
        return
    obj_table = obj_id = None
    for tbl in kg.ENTITY_TABLES:
        eid = name_to_id.get((tbl, w.object))
        if eid is not None:
            obj_table, obj_id = tbl, eid
            break
    kg.add_weak_relation(
        db,
        subject_table=subj[0],
        subject_id=subj[1],
        predicate=w.predicate,
        object_table=obj_table,
        object_id=obj_id,
        object_text=None if obj_id else w.object,
        document_id=doc_id,
        evidence=w.evidence,
        confidence=w.confidence,
    )


def _auto_promote_weak_predicates(db) -> int:
    """N≥WEAK_RELATION_PROMOTION_N 件 蓄積した weak_relations.predicate を schema_proposal 化."""
    rows = db.execute(
        "SELECT predicate, COUNT(*) n FROM weak_relations WHERE promoted_to IS NULL "
        "GROUP BY predicate HAVING n >= ?",
        (config.WEAK_RELATION_PROMOTION_N,),
    ).fetchall()
    n_proposed = 0
    for r in rows:
        # 既に同じ proposal がある場合はスキップ
        exists = db.execute(
            "SELECT 1 FROM schema_proposals WHERE kind='new_table' AND target_table=?"
            " AND status IN ('pending','approved')",
            (r["predicate"],),
        ).fetchone()
        if exists:
            continue
        kg.add_schema_proposal(
            db,
            kind="new_table",
            target_table=r["predicate"],
            proposed_ddl=(
                f"-- weak_relations.predicate={r['predicate']!r} が {r['n']} 件蓄積。"
                " 適切な junction DDL を人手で起こしてください。\n"
                f"CREATE TABLE {r['predicate']}_PROPOSED (\n"
                f"  id INTEGER PRIMARY KEY\n"
                f"  -- TODO: subject/object FK と属性列を追加\n"
                f");"
            ),
            rationale=f"auto-promotion: {r['n']} weak_relations rows",
            requires_manual_dry_run=True,
        )
        n_proposed += 1
    return n_proposed


def _render_new_table_ddl(proposal) -> str:
    cols = ",\n  ".join(
        f"{c.name} {c.type}" for c in proposal.additional_columns
    ) or "-- TODO: columns"
    return (
        f"CREATE TABLE {proposal.table} (\n"
        f"  id INTEGER PRIMARY KEY,\n"
        f"  canonical_name TEXT NOT NULL,\n"
        f"  norm_key TEXT NOT NULL UNIQUE,\n"
        f"  {cols},\n"
        f"  created_at TEXT NOT NULL,\n"
        f"  updated_at TEXT NOT NULL\n"
        f");"
    )


# ---------- main ----------
def ingest_file(path: str) -> dict:
    src = config.ROOT / path
    body = src.read_text(encoding="utf-8", errors="replace")
    slug = src.stem
    title = slug.replace("-", " ")

    chunks = semantic_chunk(body, config.CHUNK_CHARS)
    print(f"[1] semantic chunking: {src.name} → {len(chunks)} chunk(s)")

    print("[2] typed extract")
    g = extract_graph_from_chunks(chunks, source_body=body)
    print(f"  entities={len(g.entities)} relations={len(g.relations)} weak={len(g.weak_relations)}")

    db = kg.connect()
    counters: dict = {}
    with kg.writer(db):
        doc_id = kg.upsert_document(db, slug, title, str(src), body)
        # ① entities
        print("[3] resolve_or_stage (entities)")
        name_to_id: dict[tuple[str, str], int] = {}
        for e in g.entities:
            eid = _resolve_entity(db, e, doc_id, counters)
            if eid is not None:
                name_to_id[(e.proposed_type, e.canonical_name)] = eid
        # ② relations
        print("[4] resolve_relation (junctions / weak / staging)")
        for r in g.relations:
            _resolve_relation(db, r, doc_id, name_to_id, counters)
        # ③ weak
        for w in g.weak_relations:
            _route_weak_extraction(db, w, doc_id, name_to_id)
        # ④ weak → schema_proposal 昇格
        promoted = _auto_promote_weak_predicates(db)
        if promoted:
            counters["weak_promotions"] = promoted

    logadd.add(
        "ingest", title,
        f"entities={len(g.entities)} relations={len(g.relations)} weak={len(g.weak_relations)} "
        f"counters={counters}",
    )

    print("\n===== レビュー要約 =====")
    print(f"文書: {title} (slug={slug})")
    for k in sorted(counters):
        print(f"  {k}: {counters[k]}")
    print("本文冒頭:\n" + textwrap.indent(body[:600], "  "))
    return counters


def main():
    if len(sys.argv) < 2:
        print("usage: python tools/ingest.py raw/extracted/<file>.md")
        return
    ingest_file(sys.argv[1])


if __name__ == "__main__":
    main()
