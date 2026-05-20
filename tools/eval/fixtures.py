"""eval 用 fixture DB ビルダ + 本番 DB 隔離ガード。

設計の核: 本番 DB (data/kg.sqlite) には絶対に書き込まないし、評価実行中の
config.KG_DB が本番に向いていないことを kg.connect 直前にガードする。
"""
from __future__ import annotations

import uuid
from pathlib import Path

import config


ROOT = config.ROOT
PROD_DB = (ROOT / "data" / "kg.sqlite").resolve()
EVAL_ROOT = ROOT / "data" / "eval"
RUNTIME_DIR = EVAL_ROOT / "_runtime"
FIXTURES_DIR = EVAL_ROOT / "fixtures"

ALLOWED_DB_ROOTS = (RUNTIME_DIR.resolve(), FIXTURES_DIR.resolve())


def assert_not_prod(path: Path | str) -> None:
    """与えられたパスが本番 DB と一致したら即死。eval から本番 DB に触る経路を塞ぐ。"""
    p = Path(path).resolve()
    if p == PROD_DB:
        raise RuntimeError(
            f"eval は本番 DB に触れません: {p}\n"
            "config.KG_DB が本番に戻っていないか確認してください。"
        )


def assert_under_allowed(path: Path | str) -> None:
    """eval が書き込んでよいのは data/eval/_runtime か data/eval/fixtures のみ。"""
    p = Path(path).resolve()
    for root in ALLOWED_DB_ROOTS:
        try:
            p.relative_to(root)
            return
        except ValueError:
            continue
    raise RuntimeError(
        f"eval の DB 出力先は {[str(r) for r in ALLOWED_DB_ROOTS]} 配下に限ります: {p}"
    )


def ensure_dirs() -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)


def fresh_runtime_db(label: str) -> Path:
    """一時 SQLite ファイルパスを発行（プロセス終了で破棄される想定）。"""
    ensure_dirs()
    path = RUNTIME_DIR / f"{label}-{uuid.uuid4().hex[:8]}.sqlite"
    assert_not_prod(path)
    assert_under_allowed(path)
    return path


def redirect_kg_db(target: Path) -> Path:
    """config.KG_DB を eval 専用パスへ書き換え、書き換え後パスを返す。restore しない。"""
    target = Path(target).resolve()
    assert_not_prod(target)
    assert_under_allowed(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    config.KG_DB = target
    # 念のため再検証
    assert_not_prod(config.KG_DB)
    return config.KG_DB


# ---------- fixture build (typed-schema snapshot DB) ----------
def build_fixture(extract_gold_path: Path | str, out_sqlite: Path | str) -> Path:
    """extract gold YAML (schema_version >= 2) を読んで typed snapshot DB を構築する。

    gold の entities/relations を typed API (upsert_<type> / find_or_create_<junction> /
    record_claim / record_existence_claim) で投入し、決定的に同じ DB を作る。
    """
    out = Path(out_sqlite).resolve()
    assert_not_prod(out)
    assert_under_allowed(out)
    if out.exists():
        out.unlink()

    from tools.eval import io as eio
    gold = eio.load_yaml(extract_gold_path)
    slug = gold["doc_slug"]
    source_rel = gold["source"]

    redirect_kg_db(out)
    import db as kg
    body = (config.ROOT / source_rel).read_text(encoding="utf-8", errors="replace") \
        if (config.ROOT / source_rel).exists() else ""

    conn = kg.connect()
    try:
        with kg.writer(conn):
            doc_id = kg.upsert_document(conn, slug, slug.replace("-", " "), source_rel, body)
            # entities
            name_to_table_id: dict[str, tuple[str, int]] = {}
            for e in gold.get("entities") or []:
                table = e.get("proposed_type") or "person"
                if table not in kg.ENTITY_TABLES:
                    continue
                nm = e["canonical_name"]
                eid, _ = kg.upsert_entity(conn, table, nm)
                for a in e.get("aliases") or []:
                    kg.add_alias(conn, table, eid, a)
                    name_to_table_id[a] = (table, eid)
                name_to_table_id[nm] = (table, eid)
                # attributes → record_claim (gold は conf=0.9 で投入)
                cols = kg.ENTITY_CLAIM_COLUMNS[table]
                for col, val in (e.get("attributes") or {}).items():
                    if val is None or col not in cols:
                        continue
                    kg.record_claim(
                        conn, table, eid, col, val, doc_id,
                        evidence=f"gold:{slug}", confidence=0.9,
                    )
            # relations
            for r in gold.get("relations") or []:
                junction = r.get("proposed_junction")
                if junction not in kg.RELATION_CLAIM_COLUMNS:
                    continue
                f_name = r.get("from")
                t_name = r.get("to")
                src = name_to_table_id.get(f_name)
                attrs = r.get("attributes") or {}
                # compliance: to は standard 名 (テキスト)。dst は entity ではないので別経路
                if junction == "compliance":
                    if src is None or src[0] != "contract":
                        continue
                    standard = (t_name or "").strip()
                    if not standard:
                        continue
                    cid, _, _ = kg.find_or_create_compliance(
                        conn, src[1], standard, doc_id,
                        certified_until=attrs.get("certified_until"),
                        evidence=f"gold:{slug}", confidence=0.9,
                    )
                    kg.record_existence_claim(conn, "compliance", cid, doc_id,
                                              evidence=f"gold:{slug}", confidence=0.9)
                    continue
                dst = name_to_table_id.get(t_name)
                if src is None or dst is None:
                    continue
                if junction == "employment":
                    person_id = src[1] if src[0] == "person" else dst[1]
                    org_id = dst[1] if dst[0] == "organization" else src[1]
                    eid, _ = kg.find_or_create_employment(
                        conn, person_id, org_id, attrs.get("start_date"), doc_id,
                        evidence=f"gold:{slug}", confidence=0.9,
                    )
                    kg.record_existence_claim(conn, "employment", eid, doc_id,
                                              evidence=f"gold:{slug}", confidence=0.9)
                    for col in ("role", "end_date"):
                        if col in attrs and attrs[col]:
                            kg.record_relation_claim(
                                conn, "employment", eid, col, attrs[col], doc_id,
                                evidence=f"gold:{slug}", confidence=0.9,
                            )
                elif junction == "manufacturing":
                    prod_id = src[1] if src[0] == "product" else dst[1]
                    org_id = dst[1] if dst[0] == "organization" else src[1]
                    mid, _, _ = kg.find_or_create_manufacturing(
                        conn, prod_id, org_id, doc_id,
                        evidence=f"gold:{slug}", confidence=0.9,
                    )
                    kg.record_existence_claim(conn, "manufacturing", mid, doc_id,
                                              evidence=f"gold:{slug}", confidence=0.9)
                elif junction == "org_hierarchy":
                    parent_id = src[1]
                    child_id = dst[1]
                    oid, _, _ = kg.find_or_create_org_hierarchy(
                        conn, parent_id, child_id, doc_id,
                        evidence=f"gold:{slug}", confidence=0.9,
                    )
                    kg.record_existence_claim(conn, "org_hierarchy", oid, doc_id,
                                              evidence=f"gold:{slug}", confidence=0.9)
                elif junction == "product_variant":
                    parent_id = src[1]
                    variant_id = dst[1]
                    pvid, _, _ = kg.find_or_create_product_variant(
                        conn, parent_id, variant_id, doc_id,
                        evidence=f"gold:{slug}", confidence=0.9,
                    )
                    kg.record_existence_claim(conn, "product_variant", pvid, doc_id,
                                              evidence=f"gold:{slug}", confidence=0.9)
                elif junction == "governance":
                    product_id = src[1] if src[0] == "product" else dst[1]
                    contract_id = dst[1] if dst[0] == "contract" else src[1]
                    gid, _, _ = kg.find_or_create_governance(
                        conn, product_id, contract_id, doc_id,
                        evidence=f"gold:{slug}", confidence=0.9,
                    )
                    kg.record_existence_claim(conn, "governance", gid, doc_id,
                                              evidence=f"gold:{slug}", confidence=0.9)
            # weak_relations
            for w in gold.get("weak_relations") or []:
                subj = name_to_table_id.get(w.get("subject"))
                if subj is None:
                    continue
                obj = name_to_table_id.get(w.get("object"))
                kg.add_weak_relation(
                    conn,
                    subject_table=subj[0], subject_id=subj[1],
                    predicate=w["predicate"],
                    object_table=obj[0] if obj else None,
                    object_id=obj[1] if obj else None,
                    object_text=None if obj else w.get("object"),
                    document_id=doc_id,
                    evidence=f"gold:{slug}",
                    confidence=0.3,
                )
    finally:
        conn.close()
    return out
