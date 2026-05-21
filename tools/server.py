"""Typed-schema KG ダッシュボード用 API + 静的フロント配信.

  python tools/server.py        # http://127.0.0.1:8000

DB は read-only で開く (mutating endpoint は別接続で書込)。
"""
from __future__ import annotations

import json
from pathlib import Path

import config
import db as kg
import proposal_review as pr
import query as q

EVAL_RUNS_DIR = config.ROOT / "data" / "eval" / "runs"

try:
    from fastapi import Body, FastAPI
    from fastapi.responses import FileResponse, JSONResponse
    from fastapi.staticfiles import StaticFiles
    import uvicorn
except ImportError:
    raise SystemExit("pip install fastapi uvicorn を実行してください")

app = FastAPI(title="LLM Wiki Typed KG Dashboard")


def ro():
    return kg.connect(readonly=True)


def rw():
    return kg.connect(readonly=False)


# ---------- stats ----------
@app.get("/api/stats")
def stats():
    db = ro()
    g = lambda s: db.execute(s).fetchone()[0]  # noqa: E731
    counts = {tbl: g(f"SELECT COUNT(*) FROM {tbl}") for tbl in kg.ENTITY_TABLES}
    relation_counts = {tbl: g(f"SELECT COUNT(*) FROM {tbl}") for tbl in kg.RELATION_CLAIM_COLUMNS}
    return {
        "documents": g("SELECT COUNT(*) FROM documents WHERE id != 1"),  # __human__ 除外
        "entities": counts,
        "relations": relation_counts,
        "weak_relations": g("SELECT COUNT(*) FROM weak_relations WHERE promoted_to IS NULL"),
        "pending_proposals": g("SELECT COUNT(*) FROM schema_proposals WHERE status='pending'"),
        "pending_staging": g("SELECT COUNT(*) FROM staging_extractions WHERE status='pending'"),
        "open_conflicts": g("SELECT COUNT(*) FROM conflict_groups WHERE resolved_at IS NULL"),
    }


# ---------- 型別 entity 一覧 / 詳細 ----------
# 注: 2 セグメント catch-all `/api/{table}/{eid}` は他のルートを誤捕獲しがち
#   (例 `/api/eval/runs` を table='eval', eid='runs' として捕まえる)。FastAPI/
#   Starlette は path regex を直接サポートしないので、(a) 具体的ルートを先に登録、
#   (b) catch-all ハンドラ内で ENTITY_TABLES 外を 404 返却、の 2 段で防ぐ。
@app.get("/api/{table}/list")
def entity_list(table: str, q: str = ""):
    if table not in kg.ENTITY_TABLES:
        return JSONResponse({"error": "unknown table"}, status_code=404)
    db = ro()
    sql = f"SELECT id, canonical_name FROM {table}"
    args: list = []
    if q:
        sql += " WHERE canonical_name LIKE ?"
        args.append(f"%{q}%")
    sql += " ORDER BY canonical_name LIMIT 300"
    return [dict(r) for r in db.execute(sql, args)]


def _entity_relations(db, table: str, eid: int) -> list[dict]:
    """全 6 junction を統一フォーマットで返す.

    出力形式:
      {kind, id, other_table, other_id, other_canonical, role_label,
       attrs, confidence, doc_slug}
    standard_name など free-text 相手の場合 other_table=None, other_canonical=None,
    attrs に値が入る (compliance 等).
    """
    out: list[dict] = []

    def _emit(rows, *, kind, role_label, other_table, other_field,
              attr_cols=()):
        for r in rows:
            d = dict(r)
            other_id = d.get(other_field)
            attrs = {c: d.get(c) for c in attr_cols if d.get(c) is not None}
            out.append({
                "kind": kind,
                "id": d["id"],
                "other_table": other_table,
                "other_id": other_id,
                "other_canonical": d.get("other_canonical"),
                "role_label": role_label,
                "attrs": attrs,
                "confidence": d.get("confidence"),
                "doc_slug": d.get("doc_slug"),
            })

    if table == "person":
        rows = db.execute(
            "SELECT e.*, o.canonical_name AS other_canonical, d.slug AS doc_slug "
            "FROM employment e JOIN organization o ON o.id=e.organization_id "
            "JOIN documents d ON d.id=e.document_id WHERE e.person_id=?",
            (eid,),
        ).fetchall()
        _emit(rows, kind="employment", role_label="employed_by",
              other_table="organization", other_field="organization_id",
              attr_cols=("role", "start_date", "end_date"))

    elif table == "organization":
        rows = db.execute(
            "SELECT e.*, p.canonical_name AS other_canonical, d.slug AS doc_slug "
            "FROM employment e JOIN person p ON p.id=e.person_id "
            "JOIN documents d ON d.id=e.document_id WHERE e.organization_id=?",
            (eid,),
        ).fetchall()
        _emit(rows, kind="employment", role_label="employs",
              other_table="person", other_field="person_id",
              attr_cols=("role", "start_date", "end_date"))
        rows = db.execute(
            "SELECT m.*, p.canonical_name AS other_canonical, d.slug AS doc_slug "
            "FROM manufacturing m JOIN product p ON p.id=m.product_id "
            "JOIN documents d ON d.id=m.document_id WHERE m.organization_id=?",
            (eid,),
        ).fetchall()
        _emit(rows, kind="manufacturing", role_label="manufactures",
              other_table="product", other_field="product_id")
        rows = db.execute(
            "SELECT h.*, o.canonical_name AS other_canonical, d.slug AS doc_slug "
            "FROM org_hierarchy h JOIN organization o ON o.id=h.child_org_id "
            "JOIN documents d ON d.id=h.document_id WHERE h.parent_org_id=?",
            (eid,),
        ).fetchall()
        _emit(rows, kind="org_hierarchy", role_label="parent_of",
              other_table="organization", other_field="child_org_id")
        rows = db.execute(
            "SELECT h.*, o.canonical_name AS other_canonical, d.slug AS doc_slug "
            "FROM org_hierarchy h JOIN organization o ON o.id=h.parent_org_id "
            "JOIN documents d ON d.id=h.document_id WHERE h.child_org_id=?",
            (eid,),
        ).fetchall()
        _emit(rows, kind="org_hierarchy", role_label="child_of",
              other_table="organization", other_field="parent_org_id")

    elif table == "product":
        rows = db.execute(
            "SELECT m.*, o.canonical_name AS other_canonical, d.slug AS doc_slug "
            "FROM manufacturing m JOIN organization o ON o.id=m.organization_id "
            "JOIN documents d ON d.id=m.document_id WHERE m.product_id=?",
            (eid,),
        ).fetchall()
        _emit(rows, kind="manufacturing", role_label="manufactured_by",
              other_table="organization", other_field="organization_id")
        rows = db.execute(
            "SELECT v.*, p.canonical_name AS other_canonical, d.slug AS doc_slug "
            "FROM product_variant v JOIN product p ON p.id=v.variant_product_id "
            "JOIN documents d ON d.id=v.document_id WHERE v.parent_product_id=?",
            (eid,),
        ).fetchall()
        _emit(rows, kind="product_variant", role_label="parent_of",
              other_table="product", other_field="variant_product_id")
        rows = db.execute(
            "SELECT v.*, p.canonical_name AS other_canonical, d.slug AS doc_slug "
            "FROM product_variant v JOIN product p ON p.id=v.parent_product_id "
            "JOIN documents d ON d.id=v.document_id WHERE v.variant_product_id=?",
            (eid,),
        ).fetchall()
        _emit(rows, kind="product_variant", role_label="variant_of",
              other_table="product", other_field="parent_product_id")
        rows = db.execute(
            "SELECT g.*, c.canonical_name AS other_canonical, d.slug AS doc_slug "
            "FROM governance g JOIN contract c ON c.id=g.contract_id "
            "JOIN documents d ON d.id=g.document_id WHERE g.product_id=?",
            (eid,),
        ).fetchall()
        _emit(rows, kind="governance", role_label="governed_by",
              other_table="contract", other_field="contract_id")

    elif table == "contract":
        rows = db.execute(
            "SELECT g.*, p.canonical_name AS other_canonical, d.slug AS doc_slug "
            "FROM governance g JOIN product p ON p.id=g.product_id "
            "JOIN documents d ON d.id=g.document_id WHERE g.contract_id=?",
            (eid,),
        ).fetchall()
        _emit(rows, kind="governance", role_label="governs",
              other_table="product", other_field="product_id")
        rows = db.execute(
            "SELECT c.*, d.slug AS doc_slug FROM compliance c "
            "JOIN documents d ON d.id=c.document_id WHERE c.contract_id=?",
            (eid,),
        ).fetchall()
        for r in rows:
            d = dict(r)
            out.append({
                "kind": "compliance",
                "id": d["id"],
                "other_table": None,
                "other_id": None,
                "other_canonical": d.get("standard_name"),
                "role_label": "complies_with",
                "attrs": {
                    "standard_name": d.get("standard_name"),
                    **({"certified_until": d["certified_until"]}
                       if d.get("certified_until") else {}),
                },
                "confidence": d.get("confidence"),
                "doc_slug": d.get("doc_slug"),
            })

    return out


@app.get("/api/{table}/{eid:int}")
def entity_detail(table: str, eid: int):
    if table not in kg.ENTITY_TABLES:
        return JSONResponse({"error": "unknown table"}, status_code=404)
    db = ro()
    e = db.execute(f"SELECT * FROM {table} WHERE id=?", (eid,)).fetchone()
    if not e:
        return JSONResponse({"error": "not found"}, status_code=404)
    aliases = db.execute(
        f"SELECT alias FROM {table}_aliases WHERE {table}_id=?", (eid,),
    ).fetchall()
    claims = db.execute(
        f"SELECT c.column_name, c.value, c.confidence, c.status, c.created_at, d.slug AS doc_slug "
        f"FROM {table}_claims c JOIN documents d ON d.id=c.document_id "
        f"WHERE c.{table}_id=? ORDER BY c.created_at DESC",
        (eid,),
    ).fetchall()
    relations = _entity_relations(db, table, eid)
    mentions = db.execute(
        "SELECT m.surface_form, d.slug AS doc_slug FROM entity_mentions m "
        "JOIN documents d ON d.id=m.document_id "
        "WHERE m.entity_table=? AND m.entity_id=?",
        (table, eid),
    ).fetchall()
    return {
        "entity": dict(e),
        "aliases": [r["alias"] for r in aliases],
        "claims": [dict(r) for r in claims],
        "relations": relations,
        "mentions": [dict(r) for r in mentions],
    }


# ---------- conflicts ----------
@app.get("/api/conflicts")
def conflicts():
    db = ro()
    groups = kg.open_conflict_groups(db)
    out = []
    for g in groups:
        # kind から claims 表を逆引き (e.g. person_birth_date → person_claims)
        kind = g["kind"]
        members: list[dict] = []
        for table in kg.ENTITY_TABLES:
            if kind.startswith(table + "_"):
                column = kind.removeprefix(table + "_")
                members = [
                    dict(r) for r in db.execute(
                        f"SELECT c.id, c.value, c.confidence, c.status, d.slug AS doc_slug "
                        f"FROM {table}_claims c JOIN documents d ON d.id=c.document_id "
                        f"WHERE c.conflict_group=?", (g["id"],),
                    )
                ]
                break
        for rel in kg.RELATION_CLAIM_COLUMNS:
            if kind == f"{rel}_existence":
                members = [
                    dict(r) for r in db.execute(
                        f"SELECT c.id, c.confidence, c.status, d.slug AS doc_slug "
                        f"FROM {rel}_existence_claims c JOIN documents d ON d.id=c.document_id "
                        f"WHERE c.conflict_group=?", (g["id"],),
                    )
                ]
                break
        out.append({"group": g["id"], "kind": kind, "created_at": g["created_at"], "members": members})
    return out


# ---------- documents ----------
@app.get("/api/documents")
def documents():
    db = ro()
    return [
        dict(r)
        for r in db.execute(
            "SELECT slug, title, ingested_at FROM documents WHERE id != 1 "
            "ORDER BY ingested_at DESC",
        )
    ]


# ---------- proposals review ----------
@app.get("/api/proposals")
def proposals(status: str = "pending"):
    db = ro()
    return [
        dict(r) for r in db.execute(
            "SELECT * FROM schema_proposals WHERE status=? ORDER BY created_at DESC",
            (status,),
        )
    ]


@app.get("/api/proposals/{pid}/validate")
def proposal_validate(pid: int):
    db = ro()
    row = db.execute("SELECT * FROM schema_proposals WHERE id=?", (pid,)).fetchone()
    if not row:
        return JSONResponse({"error": "not found"}, status_code=404)
    ast = pr.validate_ast(row["kind"], row["target_table"], row["proposed_ddl"])
    dry = pr.dry_run(row["proposed_ddl"]) if ast.ok else None
    return {
        "ast_ok": ast.ok,
        "ast_reason": ast.reason,
        "dry_ok": dry.ok if dry else None,
        "dry_reason": dry.reason if dry else None,
        "diff": dry.diff if dry else None,
        "column_diff": pr.column_diff(dry.diff) if dry and dry.ok else None,
    }


@app.post("/api/proposals/{pid}/approve")
def proposal_approve(pid: int, payload: dict = Body(default=None)):
    db = rw()
    row = db.execute("SELECT * FROM schema_proposals WHERE id=?", (pid,)).fetchone()
    if not row:
        return JSONResponse({"error": "not found"}, status_code=404)
    if row["status"] != "pending":
        return JSONResponse({"error": f"already {row['status']}"}, status_code=409)
    ast = pr.validate_ast(row["kind"], row["target_table"], row["proposed_ddl"])
    if not ast.ok:
        db.execute(
            "UPDATE schema_proposals SET status='rejected', decided_at=?, decided_by=?, "
            "rationale=COALESCE(rationale,'') || ? WHERE id=?",
            (pr._now(), (payload or {}).get("by", "system"),
             f"\n[ast_validation_failed: {ast.reason}]", pid),
        )
        db.commit()
        return JSONResponse({"error": "ast_validation_failed", "reason": ast.reason}, status_code=400)
    dry = pr.dry_run(row["proposed_ddl"])
    if not dry.ok:
        db.execute(
            "UPDATE schema_proposals SET status='rejected', decided_at=?, decided_by=?, "
            "rationale=COALESCE(rationale,'') || ? WHERE id=?",
            (pr._now(), (payload or {}).get("by", "system"),
             f"\n[dry_run_failed: {dry.reason}]", pid),
        )
        db.commit()
        return JSONResponse({"error": "dry_run_failed", "reason": dry.reason}, status_code=400)
    name = f"proposal_{pid}_{row['kind']}_{row['target_table'] or 'na'}"
    version = pr.apply(db, pid, row["proposed_ddl"], name)
    return {"applied_migration": version, "diff": dry.diff}


@app.post("/api/proposals/{pid}/reject")
def proposal_reject(pid: int, payload: dict = Body(default=None)):
    db = rw()
    row = db.execute("SELECT status FROM schema_proposals WHERE id=?", (pid,)).fetchone()
    if not row:
        return JSONResponse({"error": "not found"}, status_code=404)
    db.execute(
        "UPDATE schema_proposals SET status='rejected', decided_at=?, decided_by=? WHERE id=?",
        (pr._now(), (payload or {}).get("by", "system"), pid),
    )
    db.commit()
    return {"ok": True}


# ---------- staging review ----------
@app.get("/api/staging")
def staging(status: str = "pending"):
    db = ro()
    return [
        dict(r) for r in db.execute(
            "SELECT s.*, d.slug AS doc_slug FROM staging_extractions s "
            "JOIN documents d ON d.id=s.document_id "
            "WHERE s.status=? ORDER BY s.created_at DESC",
            (status,),
        )
    ]


@app.post("/api/staging/{sid}/decide")
def staging_decide(sid: int, payload: dict = Body(default=None)):
    decided_table = (payload or {}).get("decided_table")
    db = rw()
    db.execute(
        "UPDATE staging_extractions SET status='assigned', decided_table=?, decided_at=? "
        "WHERE id=?",
        (decided_table, pr._now(), sid),
    )
    db.commit()
    return {"ok": True}


@app.post("/api/staging/{sid}/reject")
def staging_reject(sid: int):
    db = rw()
    db.execute(
        "UPDATE staging_extractions SET status='rejected', decided_at=? WHERE id=?",
        (pr._now(), sid),
    )
    db.commit()
    return {"ok": True}


# ---------- weak_relations triage ----------
# entity_table が動的なので polymorphic FK 解決は CASE/COALESCE で行う。
# starter set 外の table 値が入っていても安全 (どの分岐にもヒットせず NULL)。
_WEAK_ENT_TABLES = ("person", "organization", "product", "project", "contract")


def _coalesce_canonical(side: str) -> str:
    """side ∈ {'subject','object'} 用の COALESCE サブクエリ式を組み立てる."""
    parts = [
        f"(SELECT canonical_name FROM {t} WHERE w.{side}_table='{t}' AND id=w.{side}_id)"
        for t in _WEAK_ENT_TABLES
    ]
    return "COALESCE(\n    " + ",\n    ".join(parts) + "\n  )"


@app.get("/api/weak_relations")
def weak_relations(predicate: str = ""):
    db = ro()
    if predicate:
        sql = (
            "SELECT w.*, "
            f"{_coalesce_canonical('subject')} AS subject_canonical, "
            f"{_coalesce_canonical('object')} AS object_canonical, "
            "d.slug AS doc_slug "
            "FROM weak_relations w "
            "LEFT JOIN documents d ON d.id=w.document_id "
            "WHERE w.promoted_to IS NULL AND w.predicate=? "
            "ORDER BY w.created_at DESC LIMIT 500"
        )
        rows = db.execute(sql, (predicate,))
    else:
        rows = db.execute(
            "SELECT predicate, COUNT(*) n FROM weak_relations WHERE promoted_to IS NULL "
            "GROUP BY predicate ORDER BY n DESC",
        )
    return [dict(r) for r in rows]


@app.get("/api/weak_relations/matrix")
def weak_matrix():
    """predicate × subject_table × object_table の件数集計 (heatmap 用)."""
    db = ro()
    rows = db.execute(
        "SELECT predicate, subject_table, "
        "COALESCE(object_table,'(text)') AS object_table, COUNT(*) AS n "
        "FROM weak_relations WHERE promoted_to IS NULL "
        "GROUP BY predicate, subject_table, object_table "
        "ORDER BY n DESC",
    )
    return [dict(r) for r in rows]


@app.post("/api/weak_relations/promote")
def weak_promote(payload: dict = Body(...)):
    """同一 predicate を schema_proposal (new_table) に昇格させる。"""
    predicate = payload.get("predicate")
    if not predicate:
        return JSONResponse({"error": "predicate required"}, status_code=400)
    db = rw()
    kg.add_schema_proposal(
        db,
        kind="new_table",
        target_table=predicate,
        proposed_ddl=(
            f"CREATE TABLE {predicate} (\n"
            "  id INTEGER PRIMARY KEY,\n"
            "  -- TODO: subject/object FK と属性列を追加\n"
            "  created_at TEXT NOT NULL\n"
            ");"
        ),
        rationale=f"manual promotion from weak_relations: {predicate}",
        requires_manual_dry_run=True,
    )
    db.commit()
    return {"ok": True}


# ---------- eval (旧実装を流用) ----------
def _summarize_per_case(stage: str, per_case: list) -> tuple[int, bool]:
    if not per_case:
        return 0, False
    if stage == "query":
        has_fail = any(
            (c.get("error") is not None)
            or (c.get("sql_ok") is False)
            or (c.get("missed_contains"))
            or (c.get("missed_slugs"))
            for c in per_case
        )
    else:
        has_fail = True
    return len(per_case), has_fail


def _headline(stage: str, agg: dict) -> tuple[dict, float | None]:
    g = lambda k: agg.get(k + "_mean")  # noqa: E731
    if stage == "query":
        vals = {"sql": g("sql_success_rate"), "row": g("row_contains_rate"), "doc": g("doc_slug_rate")}
    elif stage == "extract":
        vals = {"ent_f1": g("entities_f1"), "rel_f1": g("relations_f1"), "fact_f1": g("facts_f1")}
    elif stage == "dedup":
        vals = {"acc": g("accuracy"), "f1": g("f1"), "lvl": g("level_match_rate")}
    elif stage == "proposal":
        vals = {
            "prop_f1":  g("proposal_f1"),
            "stage_f1": g("staging_f1"),
            "weak_r":   g("weak_promotion_recall"),
            "stab":     g("canonical_stability_passed"),
        }
    else:
        vals = {k[:-5]: v for k, v in agg.items() if k.endswith("_mean")}
    nums = [v for v in vals.values() if isinstance(v, (int, float))]
    score = (sum(nums) / len(nums)) if nums else None
    return vals, score


@app.get("/api/eval/runs")
def eval_runs():
    if not EVAL_RUNS_DIR.is_dir():
        return []
    items = []
    for p in sorted(EVAL_RUNS_DIR.glob("*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            meta = data.get("meta", {}) or {}
            result = data.get("result", {}) or {}
            stage = meta.get("stage") or "unknown"
            per_case = result.get("per_case") or []
            n_case, has_fail = _summarize_per_case(stage, per_case)
            agg = result.get("aggregate") or {}
            headline, score = _headline(stage, agg)
            items.append({
                "filename": p.name, "meta": meta, "aggregate": agg,
                "gold_summary": result.get("gold_summary") or {},
                "per_case_count": n_case, "has_failures": has_fail,
                "headline": headline, "score": score,
            })
        except (OSError, json.JSONDecodeError, ValueError) as e:
            items.append({"filename": p.name, "error": str(e)})
    items.sort(
        key=lambda x: (x.get("meta", {}).get("timestamp_utc") or "", x["filename"]),
        reverse=True,
    )
    return items


@app.get("/api/eval/run/{filename}")
def eval_run(filename: str):
    if not filename.endswith(".json") or Path(filename).name != filename:
        return JSONResponse({"error": "invalid filename"}, status_code=400)
    target = (EVAL_RUNS_DIR / filename).resolve()
    try:
        inside = target.is_relative_to(EVAL_RUNS_DIR.resolve())
    except AttributeError:
        inside = str(target).startswith(str(EVAL_RUNS_DIR.resolve()))
    if not inside or not target.is_file():
        return JSONResponse({"error": "not found"}, status_code=404)
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ---------- ask (typed schema 経由) ----------
@app.post("/api/ask")
def ask(payload: dict = Body(...)):
    question = (payload or {}).get("question", "").strip()
    if not question:
        return JSONResponse({"error": "question is empty"}, status_code=400)
    return q.answer_question(question)


@app.get("/")
def index():
    return FileResponse(config.WEB_DIR / "index.html")


def main():
    if not config.KG_DB.exists():
        kg.connect().commit()
    try:
        app.mount("/static", StaticFiles(directory=config.WEB_DIR), name="static")
    except Exception:  # noqa: BLE001
        pass
    print(f"→ http://{config.HOST}:{config.PORT}")
    uvicorn.run(app, host=config.HOST, port=config.PORT, log_level="warning")


if __name__ == "__main__":
    main()
