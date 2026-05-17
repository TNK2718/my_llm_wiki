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


# ---------- fixture build（query 評価用 snapshot DB） ----------
def build_fixture(extract_gold_path: Path | str, out_sqlite: Path | str) -> Path:
    """extract gold YAML を読んで、決定的にスナップショット DB を構築する。

    LLM は経由せず、gold の entities/relations/facts を直接 INSERT。
    query 評価がモデル出力に依存しない再現性を持つ。
    """
    import sqlite3
    from datetime import date

    from tools.eval import io as eio

    # 出力先を安全配下に強制
    out = Path(out_sqlite).resolve()
    assert_not_prod(out)
    assert_under_allowed(out)
    if out.exists():
        out.unlink()

    gold = eio.load_yaml(extract_gold_path)
    slug = gold["doc_slug"]
    source_rel = gold["source"]

    # connect は config.KG_DB を見るので一時的に out へ向ける
    redirect_kg_db(out)
    import db as kg  # 遅延 import: redirect 後に確実に新規 DB を作る

    conn = sqlite3.connect(out)
    conn.executescript(config.SCHEMA_SQL.read_text(encoding="utf-8"))
    conn.row_factory = sqlite3.Row
    today = date.today().isoformat()
    body = (config.ROOT / source_rel).read_text(encoding="utf-8", errors="replace") if (config.ROOT / source_rel).exists() else ""

    # document
    conn.execute(
        "INSERT INTO documents(slug,title,path,ingested_at) VALUES(?,?,?,?)",
        (slug, slug.replace("-", " "), source_rel, today),
    )
    conn.execute("INSERT INTO doc_fts(slug,title,body) VALUES(?,?,?)", (slug, slug, body))
    doc_id = conn.execute("SELECT id FROM documents WHERE slug=?", (slug,)).fetchone()["id"]

    # entities + aliases
    name_to_id: dict[str, int] = {}
    for e in gold.get("entities") or []:
        nm = e["name"]
        et = e.get("type") or "concept"
        nk = kg.normalize(nm)
        try:
            cur = conn.execute(
                "INSERT INTO entities(canonical_name,type,norm_key,attributes,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?)",
                (nm, et, nk, "{}", today, today),
            )
            eid = cur.lastrowid
        except sqlite3.IntegrityError:
            eid = conn.execute(
                "SELECT id FROM entities WHERE type=? AND norm_key=?", (et, nk)
            ).fetchone()["id"]
        name_to_id[nm] = eid
        conn.execute(
            "INSERT OR IGNORE INTO entity_aliases(entity_id,alias,norm_key) VALUES(?,?,?)",
            (eid, nm, nk),
        )
        for a in e.get("aliases") or []:
            conn.execute(
                "INSERT OR IGNORE INTO entity_aliases(entity_id,alias,norm_key) VALUES(?,?,?)",
                (eid, a, kg.normalize(a)),
            )

    def _resolve_name(n: str) -> int:
        if n in name_to_id:
            return name_to_id[n]
        # alias 経由で引く（gold relations が alias 表記でも繋ぐため）
        row = conn.execute(
            "SELECT entity_id FROM entity_aliases WHERE norm_key=?", (kg.normalize(n),)
        ).fetchone()
        if row:
            return row["entity_id"]
        # 解決不能 → concept として新規作成
        cur = conn.execute(
            "INSERT INTO entities(canonical_name,type,norm_key,attributes,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?)",
            (n, "concept", kg.normalize(n), "{}", today, today),
        )
        name_to_id[n] = cur.lastrowid
        return cur.lastrowid

    # relations
    for r in gold.get("relations") or []:
        s, p, o = r.get("subject"), r.get("predicate"), r.get("object")
        if not (s and p and o):
            continue
        conn.execute(
            "INSERT INTO relations(subject_id,predicate,object_id,document_id,evidence,confidence,created_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (_resolve_name(s), p, _resolve_name(o), doc_id, r.get("evidence", ""), 0.9, today),
        )

    # facts
    for f in gold.get("facts") or []:
        e, a, v = f.get("entity"), f.get("attribute"), f.get("value")
        if not (e and a):
            continue
        conn.execute(
            "INSERT INTO facts(entity_id,attribute,value,document_id,created_at) VALUES(?,?,?,?,?)",
            (_resolve_name(e), a, v, doc_id, today),
        )

    conn.commit()
    conn.close()
    return out
