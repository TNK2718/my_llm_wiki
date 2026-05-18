"""ダッシュボード用 API + 静的フロント配信。

  pip install fastapi uvicorn
  python tools/server.py        # http://127.0.0.1:8000

DB は read-only で開く。/api/ask のみ Ollama を使う（未起動なら error フィールドで通知）。
"""
import sqlite3

import config
import db as kg
import query as q

try:
    from fastapi import FastAPI, Body
    from fastapi.responses import FileResponse, JSONResponse
    from fastapi.staticfiles import StaticFiles
    import uvicorn
except ImportError:
    raise SystemExit("pip install fastapi uvicorn を実行してください")

app = FastAPI(title="LLM Wiki KG Dashboard")


def ro():
    return kg.connect(readonly=True)


@app.get("/api/stats")
def stats():
    db = ro()
    g = lambda s: db.execute(s).fetchone()[0]  # noqa: E731
    rel_c, fact_c = kg.open_conflicts(db)
    return {
        "documents": g("SELECT COUNT(*) FROM documents"),
        "entities": g("SELECT COUNT(*) FROM entities WHERE status!='merged'"),
        "relations": g("SELECT COUNT(*) FROM relations"),
        "facts": g("SELECT COUNT(*) FROM facts"),
        "conflicts": len(rel_c) + len(fact_c),
        "by_type": [
            dict(r)
            for r in db.execute(
                "SELECT type, COUNT(*) n FROM entities WHERE status!='merged' "
                "GROUP BY type ORDER BY n DESC"
            )
        ],
    }


@app.get("/api/entities")
def entities(q: str = "", type: str = ""):
    db = ro()
    sql = "SELECT id, canonical_name, type, status FROM entities WHERE status!='merged'"
    args = []
    if q:
        sql += " AND canonical_name LIKE ?"
        args.append(f"%{q}%")
    if type:
        sql += " AND type=?"
        args.append(type)
    sql += " ORDER BY canonical_name LIMIT 300"
    return [dict(r) for r in db.execute(sql, args)]


@app.get("/api/entity/{eid}")
def entity(eid: int):
    db = ro()
    e = db.execute("SELECT * FROM entities WHERE id=?", (eid,)).fetchone()
    if not e:
        return JSONResponse({"error": "not found"}, status_code=404)
    rel = db.execute(
        "SELECT r.predicate p, e2.canonical_name o, e2.id oid, d.slug src, r.status st "
        "FROM relations r JOIN entities e2 ON r.object_id=e2.id "
        "JOIN documents d ON r.document_id=d.id WHERE r.subject_id=?",
        (eid,),
    ).fetchall()
    inc = db.execute(
        "SELECT r.predicate p, e1.canonical_name s, e1.id sid, d.slug src, r.status st "
        "FROM relations r JOIN entities e1 ON r.subject_id=e1.id "
        "JOIN documents d ON r.document_id=d.id WHERE r.object_id=?",
        (eid,),
    ).fetchall()
    facts = db.execute(
        "SELECT f.attribute a, f.value v, d.slug src, f.status st "
        "FROM facts f JOIN documents d ON f.document_id=d.id WHERE f.entity_id=?",
        (eid,),
    ).fetchall()
    aliases = db.execute(
        "SELECT alias FROM entity_aliases WHERE entity_id=?", (eid,)
    ).fetchall()
    return {
        "entity": dict(e),
        "out": [dict(r) for r in rel],
        "in": [dict(r) for r in inc],
        "facts": [dict(r) for r in facts],
        "aliases": [r["alias"] for r in aliases],
    }


@app.get("/api/graph")
def graph(focus: int = 0, limit: int = 250):
    db = ro()
    if focus:
        rels = db.execute(
            "SELECT subject_id s, object_id o, predicate p, status st FROM relations "
            "WHERE subject_id=? OR object_id=? LIMIT ?",
            (focus, focus, limit),
        ).fetchall()
    else:
        rels = db.execute(
            "SELECT subject_id s, object_id o, predicate p, status st "
            "FROM relations LIMIT ?",
            (limit,),
        ).fetchall()
    ids = {r["s"] for r in rels} | {r["o"] for r in rels} | ({focus} if focus else set())
    nodes = []
    if ids:
        ph = ",".join("?" * len(ids))
        for n in db.execute(
            f"SELECT id, canonical_name, type FROM entities WHERE id IN ({ph})",
            tuple(ids),
        ):
            nodes.append(
                {"data": {"id": str(n["id"]), "label": n["canonical_name"], "type": n["type"]}}
            )
    edges = [
        {
            "data": {
                "source": str(r["s"]),
                "target": str(r["o"]),
                "label": r["p"],
                "conflict": r["st"] == "conflicted",
            }
        }
        for r in rels
    ]
    return {"nodes": nodes, "edges": edges}


@app.get("/api/conflicts")
def conflicts():
    db = ro()
    rel_groups, fact_groups = kg.open_conflicts(db)
    out = {"relations": [], "facts": []}
    for c in rel_groups:
        members = db.execute(
            "SELECT e1.canonical_name s, r.predicate p, e2.canonical_name o, d.slug src "
            "FROM relations r JOIN entities e1 ON r.subject_id=e1.id "
            "JOIN entities e2 ON r.object_id=e2.id JOIN documents d ON r.document_id=d.id "
            "WHERE r.conflict_group=?",
            (c["conflict_group"],),
        ).fetchall()
        out["relations"].append({"group": c["conflict_group"], "members": [dict(m) for m in members]})
    for c in fact_groups:
        members = db.execute(
            "SELECT e.canonical_name n, f.attribute a, f.value v, d.slug src "
            "FROM facts f JOIN entities e ON f.entity_id=e.id "
            "JOIN documents d ON f.document_id=d.id WHERE f.conflict_group=?",
            (c["conflict_group"],),
        ).fetchall()
        out["facts"].append({"group": c["conflict_group"], "members": [dict(m) for m in members]})
    return out


@app.get("/api/documents")
def documents():
    db = ro()
    return [
        dict(r)
        for r in db.execute(
            "SELECT slug, title, ingested_at FROM documents ORDER BY ingested_at DESC"
        )
    ]


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
        kg.connect().commit()  # 空 DB を作っておく
    try:
        app.mount("/static", StaticFiles(directory=config.WEB_DIR), name="static")
    except Exception:  # noqa: BLE001
        pass
    print(f"→ http://{config.HOST}:{config.PORT}")
    uvicorn.run(app, host=config.HOST, port=config.PORT, log_level="warning")


if __name__ == "__main__":
    main()
