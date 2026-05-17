"""ナレッジグラフ DB 層。突合・矛盾管理はここに集約（決定的ルール中心、曖昧時のみ LLM）。"""
import json
import re
import sqlite3
import unicodedata
from datetime import date

import config


# ---------- 接続 ----------
def connect(readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        db = sqlite3.connect(f"file:{config.KG_DB}?mode=ro", uri=True)
    else:
        config.KG_DB.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(config.KG_DB)
        db.executescript(config.SCHEMA_SQL.read_text(encoding="utf-8"))
    db.row_factory = sqlite3.Row
    return db


# ---------- 正規化（決定的） ----------
def normalize(name: str) -> str:
    s = unicodedata.normalize("NFKC", name or "").lower().strip()
    for tok in sorted(config.STRIP_TOKENS, key=len, reverse=True):
        s = s.replace(tok.lower(), "")
    s = re.sub(r"[\s\u3000_,.\-・()（）]+", "", s)
    return s


def _trigrams(s: str) -> set[str]:
    s = f"  {s} "
    return {s[i : i + 3] for i in range(len(s) - 2)}


def similarity(a: str, b: str) -> float:
    """正規化キー同士の Jaccard トリグラム類似（0-1）。安価な近似突合。"""
    if not a or not b:
        return 0.0
    ta, tb = _trigrams(a), _trigrams(b)
    return len(ta & tb) / len(ta | tb) if ta | tb else 0.0


# ---------- 文書 ----------
def upsert_document(db, slug, title, path, summary, body) -> int:
    db.execute(
        "INSERT OR REPLACE INTO documents(slug,title,path,summary,ingested_at) "
        "VALUES(?,?,?,?,?)",
        (slug, title, path, summary, date.today().isoformat()),
    )
    db.execute("INSERT INTO doc_fts(slug,title,body) VALUES(?,?,?)", (slug, title, body))
    return db.execute("SELECT id FROM documents WHERE slug=?", (slug,)).fetchone()[0]


# ---------- 実体の突合（rule -> 曖昧なら LLM） ----------
def find_or_stage_entity(db, name: str, etype: str, adjudicate=None) -> tuple[int, str]:
    """戻り値: (entity_id, how)  how ∈ {exact, alias, llm-merge, new}"""
    nk = normalize(name)

    row = db.execute(
        "SELECT id FROM entities WHERE type=? AND norm_key=? AND status!='merged'",
        (etype, nk),
    ).fetchone()
    if row:
        return row["id"], "exact"

    row = db.execute(
        "SELECT entity_id FROM entity_aliases WHERE norm_key=?", (nk,)
    ).fetchone()
    if row:
        return _resolve(db, row["entity_id"]), "alias"

    # 近接候補（同 type のみ）をルールで絞り、しきい値以上だけ LLM 判定にかける
    best, best_sim = None, 0.0
    for r in db.execute(
        "SELECT id, canonical_name, norm_key FROM entities WHERE type=? AND status!='merged'",
        (etype,),
    ):
        sim = similarity(nk, r["norm_key"])
        if sim > best_sim:
            best, best_sim = r, sim
    if best and best_sim >= config.DEDUP_SIM_THRESHOLD and adjudicate is not None:
        if adjudicate(name, best["canonical_name"], etype) == "same":
            add_alias(db, best["id"], name)
            return best["id"], "llm-merge"

    return _create_entity(db, name, etype, nk), "new"


def _create_entity(db, name, etype, nk) -> int:
    today = date.today().isoformat()
    cur = db.execute(
        "INSERT INTO entities(canonical_name,type,norm_key,attributes,created_at,updated_at)"
        " VALUES(?,?,?,?,?,?)",
        (name, etype, nk, "{}", today, today),
    )
    eid = cur.lastrowid
    add_alias(db, eid, name)
    return eid


def add_alias(db, entity_id, alias):
    db.execute(
        "INSERT OR IGNORE INTO entity_aliases(entity_id,alias,norm_key) VALUES(?,?,?)",
        (entity_id, alias, normalize(alias)),
    )


def _resolve(db, eid) -> int:
    seen = set()
    while eid not in seen:
        seen.add(eid)
        row = db.execute("SELECT merged_into FROM entities WHERE id=?", (eid,)).fetchone()
        if not row or row["merged_into"] is None:
            return eid
        eid = row["merged_into"]
    return eid


# ---------- 関係・事実・矛盾検出（決定的） ----------
def add_relation(db, subj, pred, obj, doc_id, evidence, confidence=0.6):
    cur = db.execute(
        "INSERT INTO relations(subject_id,predicate,object_id,document_id,evidence,"
        "confidence,created_at) VALUES(?,?,?,?,?,?,?)",
        (subj, pred, obj, doc_id, evidence, confidence, date.today().isoformat()),
    )
    rid = cur.lastrowid
    # 関数的述語に別 object が既存 → 矛盾グループ化（非破壊）
    if pred in config.FUNCTIONAL_PREDICATES:
        others = db.execute(
            "SELECT id, object_id FROM relations WHERE subject_id=? AND predicate=? "
            "AND object_id!=? AND status='active'",
            (subj, pred, obj),
        ).fetchall()
        if others:
            grp = rid
            ids = [rid] + [o["id"] for o in others]
            db.executemany(
                "UPDATE relations SET status='conflicted', conflict_group=? WHERE id=?",
                [(grp, i) for i in ids],
            )
            return rid, "conflict"
    return rid, "ok"


def add_fact(db, entity_id, attr, value, doc_id):
    cur = db.execute(
        "INSERT INTO facts(entity_id,attribute,value,document_id,created_at) "
        "VALUES(?,?,?,?,?)",
        (entity_id, attr, value, doc_id, date.today().isoformat()),
    )
    fid = cur.lastrowid
    others = db.execute(
        "SELECT id FROM facts WHERE entity_id=? AND attribute=? AND value!=? "
        "AND status='active'",
        (entity_id, attr, value),
    ).fetchall()
    if others:
        grp = fid
        db.executemany(
            "UPDATE facts SET status='conflicted', conflict_group=? WHERE id=?",
            [(grp, i) for i in [fid] + [o["id"] for o in others]],
        )
        return fid, "conflict"
    return fid, "ok"


def add_mention(db, entity_id, doc_id, surface, context):
    db.execute(
        "INSERT INTO mentions(entity_id,document_id,surface_form,context) VALUES(?,?,?,?)",
        (entity_id, doc_id, surface, context),
    )


def open_conflicts(db):
    rel = db.execute(
        "SELECT conflict_group, predicate, COUNT(*) n FROM relations "
        "WHERE status='conflicted' GROUP BY conflict_group"
    ).fetchall()
    fact = db.execute(
        "SELECT conflict_group, attribute, COUNT(*) n FROM facts "
        "WHERE status='conflicted' GROUP BY conflict_group"
    ).fetchall()
    return rel, fact
