"""Typed-schema KG DB 層。docs/typed-schema-design.md §3,§5,§6,§7 を実装する。

- canonical 表は「最高 confidence active claim」のマテリアライズドビュー
- claims/existence_claims は全主張ログ + 矛盾管理
- 書き込みは BEGIN IMMEDIATE で serialize
- LLM 抽出 conf は呼び出し元で cap (LLM_CONFIDENCE_CAP)
- 低 conf は 'rejected_low_confidence' を返し、呼び出し元が staging/weak へ振り分け
"""
from __future__ import annotations

import contextlib
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Literal, Optional

import config
from normalize import normalize, similarity  # re-export for callers


# ---------- 型定数 ----------
EntityTable = Literal["person", "organization", "product", "project"]
RelationTable = Literal["employment", "manufacturing", "org_hierarchy"]

ENTITY_TABLES: tuple[str, ...] = ("person", "organization", "product", "project")

# canonical 列のうち claims で扱う列名（識別キー列は除外）
ENTITY_CLAIM_COLUMNS: dict[str, tuple[str, ...]] = {
    "person":       ("canonical_name", "birth_date", "nationality"),
    "organization": ("canonical_name", "org_type", "founded_year", "headquarters"),
    "product":      ("canonical_name", "release_date", "category"),
    "project":      ("canonical_name", "started_at", "ended_at"),
}

# junction の属性 claim 列（identification key は含めない）
RELATION_CLAIM_COLUMNS: dict[str, tuple[str, ...]] = {
    "employment":    ("role", "end_date"),
    "manufacturing": (),
    "org_hierarchy": (),
}

# canonical 列名 → conflict_kinds.kind
def _conflict_kind(table: str, column: Optional[str], *, existence: bool = False) -> str:
    if existence:
        return f"{table}_existence"
    assert column is not None
    return f"{table}_{column}"


class RecordClaimResult(str, Enum):
    INSERTED_ACTIVE         = "inserted_active"
    INSERTED_SUPERSEDED     = "inserted_superseded"
    INSERTED_CONFLICTED     = "inserted_conflicted"
    UPDATED_EVIDENCE        = "updated_evidence"
    UPSERT_REVALUE          = "upsert_revalue"
    REJECTED_LOW_CONFIDENCE = "rejected_low_confidence"


@dataclass
class ClaimDispatch:
    """record_claim/existence の戻り値。呼び出し元が staging/weak 振り分けに使う。"""
    result: RecordClaimResult
    claim_id: Optional[int]
    canonical_updated: bool


# ---------- 接続 + migration ----------
def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        db = sqlite3.connect(f"file:{config.KG_DB}?mode=ro", uri=True)
    else:
        config.KG_DB.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(config.KG_DB)
        db.executescript(config.SCHEMA_SQL.read_text(encoding="utf-8"))
    # PRAGMA は schema head の宣言だけでは接続に効かないので毎回発行する
    db.execute("PRAGMA foreign_keys = ON")
    db.row_factory = sqlite3.Row
    if not readonly:
        apply_migrations(db)
    return db


def apply_migrations(db: sqlite3.Connection) -> list[int]:
    """tools/migrations/NNNN_*.sql のうち未適用を昇順で apply。適用済 version の list を返す。"""
    applied = {r["version"] for r in db.execute("SELECT version FROM schema_migrations")}
    files = sorted(Path(config.MIGRATIONS_DIR).glob("*.sql"))
    newly = []
    for f in files:
        stem = f.stem  # e.g. "0001_bootstrap"
        try:
            version = int(stem.split("_", 1)[0])
        except ValueError:
            continue
        if version in applied:
            continue
        name = stem.split("_", 1)[1] if "_" in stem else stem
        with db:  # transaction
            db.executescript(f.read_text(encoding="utf-8"))
            db.execute(
                "INSERT INTO schema_migrations(version, name, applied_at) VALUES (?,?,?)",
                (version, name, _now()),
            )
        newly.append(version)
    return newly


@contextlib.contextmanager
def writer(db: sqlite3.Connection):
    """BEGIN IMMEDIATE で書き込みを serialize。SQLite single-writer の規約に従う。"""
    db.execute("BEGIN IMMEDIATE")
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    else:
        db.commit()


# ---------- 文書 ----------
def upsert_document(db: sqlite3.Connection, slug: str, title: str, path: Optional[str], body: str) -> int:
    """slug ベースで find-or-update。doc_fts は trigger で同期されるので触らない。"""
    row = db.execute("SELECT id FROM documents WHERE slug=?", (slug,)).fetchone()
    now = _now()
    if row:
        db.execute(
            "UPDATE documents SET title=?, path=?, body=?, ingested_at=? WHERE id=?",
            (title, path, body, now, row["id"]),
        )
        return row["id"]
    cur = db.execute(
        "INSERT INTO documents(slug,title,path,body,ingested_at) VALUES (?,?,?,?,?)",
        (slug, title, path, body, now),
    )
    return cur.lastrowid


# ---------- エンティティ upsert ----------
def upsert_entity(
    db: sqlite3.Connection,
    table: str,
    canonical_name: str,
    *,
    extra: Optional[dict] = None,
) -> tuple[int, str]:
    """norm_key で find-or-create。戻り値: (id, how) where how ∈ {exact, alias, new}.

    canonical 表は claims から派生するので、ここでは canonical_name と norm_key を
    set するだけ。他の列 (birth_date, founded_year, ...) は record_claim() 経由で
    §3 状態遷移を通って反映される。

    extra は initial seed 用（人手作成時など）。値は claim を経由しないので
    通常 ingest では None。
    """
    if table not in ENTITY_TABLES:
        raise ValueError(f"unknown entity table: {table}")
    nk = normalize(canonical_name)
    if not nk:
        raise ValueError(f"empty norm_key for {canonical_name!r}")

    row = db.execute(f"SELECT id FROM {table} WHERE norm_key=?", (nk,)).fetchone()
    if row:
        return row["id"], "exact"

    # alias 表もチェック
    alias_row = db.execute(
        f"SELECT {table}_id AS id FROM {table}_aliases WHERE norm_key=?", (nk,)
    ).fetchone()
    if alias_row:
        return alias_row["id"], "alias"

    now = _now()
    cur = db.execute(
        f"INSERT INTO {table}(canonical_name, norm_key, created_at, updated_at) VALUES (?,?,?,?)",
        (canonical_name, nk, now, now),
    )
    eid = cur.lastrowid
    db.execute(
        f"INSERT INTO {table}_aliases({table}_id, alias, norm_key) VALUES (?,?,?)",
        (eid, canonical_name, nk),
    )
    return eid, "new"


def add_alias(db: sqlite3.Connection, table: str, entity_id: int, alias: str) -> None:
    if table not in ENTITY_TABLES:
        raise ValueError(f"unknown entity table: {table}")
    db.execute(
        f"INSERT OR IGNORE INTO {table}_aliases({table}_id, alias, norm_key) VALUES (?,?,?)",
        (entity_id, alias, normalize(alias)),
    )


# ---------- 既存マッチ候補列挙（決定論的類似度） ----------
def candidate_entities_by_similarity(
    db: sqlite3.Connection,
    table: str,
    name: str,
    *,
    top_k: int = 5,
    min_sim: float = 0.3,
) -> list[dict]:
    """設計柱 D1 の決定論的類似度算出。canonical_name と aliases を走査し、

    1. norm_key 完全一致を similarity=1.0, norm_key_match=True で最優先
    2. 続いて name の trigram Jaccard で similarity >= min_sim の上位 top_k 件

    を返す。各 dict: {id, canonical_name, norm_key, similarity, norm_key_match}.
    候補ゼロなら []。
    """
    if table not in ENTITY_TABLES:
        raise ValueError(f"unknown entity table: {table}")
    if not name:
        return []
    nk = normalize(name)
    rows = db.execute(
        f"SELECT id, canonical_name, norm_key FROM {table}"
    ).fetchall()
    if not rows:
        return []

    alias_idx: dict[int, list[str]] = {}
    for ar in db.execute(
        f"SELECT {table}_id AS id, norm_key FROM {table}_aliases"
    ).fetchall():
        alias_idx.setdefault(ar["id"], []).append(ar["norm_key"])

    out: list[dict] = []
    for r in rows:
        norm_keys = [r["norm_key"]] + alias_idx.get(r["id"], [])
        norm_match = any(k == nk for k in norm_keys if k)
        sim = max((similarity(nk, k) for k in norm_keys if k), default=0.0)
        if norm_match:
            sim = 1.0
        if not norm_match and sim < min_sim:
            continue
        out.append({
            "id": r["id"],
            "canonical_name": r["canonical_name"],
            "norm_key": r["norm_key"],
            "similarity": round(sim, 4),
            "norm_key_match": norm_match,
        })
    out.sort(key=lambda d: (not d["norm_key_match"], -d["similarity"]))
    return out[:top_k]


def candidate_entities_across_tables(
    db: sqlite3.Connection,
    name: str,
    *,
    top_k_per_table: int = 3,
    min_sim: float = 0.3,
) -> list[dict]:
    """starter 全 entity table を横断して候補を返す。

    未知型 staging で「既存の似た entity がどの型に居るか」を提示するための関数。
    各 dict: {table, id, canonical_name, similarity, norm_key_match}.
    """
    out: list[dict] = []
    for tbl in ENTITY_TABLES:
        for c in candidate_entities_by_similarity(
            db, tbl, name, top_k=top_k_per_table, min_sim=min_sim
        ):
            out.append({"table": tbl, **c})
    out.sort(key=lambda d: (not d["norm_key_match"], -d["similarity"]))
    return out


# ---------- canonical 反映ヘルパ ----------
def _is_entity_table(table: str) -> bool:
    return table in ENTITY_TABLES


def _canonical_value(db: sqlite3.Connection, table: str, entity_id: int, column: str):
    row = db.execute(f"SELECT {column} FROM {table} WHERE id=?", (entity_id,)).fetchone()
    return None if row is None else row[column]


def _set_canonical(db: sqlite3.Connection, table: str, entity_id: int, column: str, value) -> None:
    now = _now()
    db.execute(
        f"UPDATE {table} SET {column}=?, updated_at=? WHERE id=?",
        (value, now, entity_id),
    )


# ---------- entity claim 状態遷移 (§3, §6) ----------
def record_claim(
    db: sqlite3.Connection,
    table: str,
    entity_id: int,
    column: str,
    value,
    document_id: int,
    *,
    evidence: Optional[str] = None,
    confidence: float = 0.5,
) -> ClaimDispatch:
    """entity 属性 claim を記録し §3 状態遷移を適用。

    呼び出し元 (ingest) は confidence を min(raw, LLM_CONFIDENCE_CAP) で cap してから渡す。
    人手 verdict は document_id=HUMAN_DOCUMENT_ID, confidence=1.0 で渡す。
    """
    if table not in ENTITY_CLAIM_COLUMNS:
        raise ValueError(f"unknown entity table: {table}")
    if column not in ENTITY_CLAIM_COLUMNS[table]:
        raise ValueError(f"{table} has no claim column {column!r}")
    if not (0 <= confidence <= 1):
        raise ValueError(f"confidence {confidence} out of range")

    if confidence < config.LOW_CONFIDENCE_THRESHOLD:
        return ClaimDispatch(RecordClaimResult.REJECTED_LOW_CONFIDENCE, None, False)

    claims_table = f"{table}_claims"
    fk = f"{table}_id"
    now = _now()
    value_norm = _coerce_value(value)

    # §6 UPSERT: 既存 (entity, column, document_id) 行があれば evidence/conf 更新か全置換
    existing = db.execute(
        f"SELECT id, value, confidence FROM {claims_table} "
        f"WHERE {fk}=? AND column_name=? AND document_id=?",
        (entity_id, column, document_id),
    ).fetchone()

    if existing is not None:
        if _values_equal(existing["value"], value_norm):
            db.execute(
                f"UPDATE {claims_table} SET evidence=?, confidence=? WHERE id=?",
                (evidence, confidence, existing["id"]),
            )
            # canonical は §3 で同列の他主張に影響されうるので再計算
            canonical_updated = _reapply_state_machine(
                db, table, entity_id, column,
            )
            return ClaimDispatch(RecordClaimResult.UPDATED_EVIDENCE, existing["id"], canonical_updated)
        # 異 value 再 ingest: 全列 UPDATE + 当該 (entity, column) の §3 再計算
        db.execute(
            f"UPDATE {claims_table} SET value=?, evidence=?, confidence=?, "
            f"status='active', conflict_group=NULL WHERE id=?",
            (value_norm, evidence, confidence, existing["id"]),
        )
        canonical_updated = _reapply_state_machine(db, table, entity_id, column)
        return ClaimDispatch(RecordClaimResult.UPSERT_REVALUE, existing["id"], canonical_updated)

    # 新規 INSERT
    cur = db.execute(
        f"INSERT INTO {claims_table}"
        f"({fk}, column_name, value, document_id, evidence, confidence, status, created_at) "
        f"VALUES (?,?,?,?,?,?,'active',?)",
        (entity_id, column, value_norm, document_id, evidence, confidence, now),
    )
    new_claim_id = cur.lastrowid
    result, canonical_updated = _apply_state_machine_for_new_claim(
        db, table, entity_id, column, new_claim_id,
    )
    return ClaimDispatch(result, new_claim_id, canonical_updated)


def _coerce_value(value) -> Optional[str]:
    if value is None:
        return None
    return str(value)


def _values_equal(a, b) -> bool:
    return _coerce_value(a) == _coerce_value(b)


def _apply_state_machine_for_new_claim(
    db: sqlite3.Connection,
    table: str,
    entity_id: int,
    column: str,
    new_claim_id: int,
) -> tuple[RecordClaimResult, bool]:
    """新 claim 1 件挿入後の §3 判定。canonical 更新の有無を返す。"""
    claims_table = f"{table}_claims"
    fk = f"{table}_id"

    new_row = db.execute(
        f"SELECT id, value, confidence FROM {claims_table} WHERE id=?", (new_claim_id,)
    ).fetchone()
    others = db.execute(
        f"SELECT id, value, confidence, status FROM {claims_table} "
        f"WHERE {fk}=? AND column_name=? AND id != ? AND status='active'",
        (entity_id, column, new_claim_id),
    ).fetchall()

    if not others:
        # 1 件目
        _set_canonical(db, table, entity_id, column, new_row["value"])
        return RecordClaimResult.INSERTED_ACTIVE, True

    # 同値 active が既にあれば multi-evidence。canonical 据え置き
    if any(_values_equal(o["value"], new_row["value"]) for o in others):
        return RecordClaimResult.INSERTED_ACTIVE, False

    # 異 value: 既存最高 conf active と比較
    delta = config.CLAIM_CONFIDENCE_DELTA
    max_other = max(others, key=lambda o: o["confidence"])
    diff = new_row["confidence"] - max_other["confidence"]

    if diff > delta:
        # 新が上回り: 既存 active を全て superseded、canonical UPDATE
        for o in others:
            db.execute(
                f"UPDATE {claims_table} SET status='superseded' WHERE id=?",
                (o["id"],),
            )
        _set_canonical(db, table, entity_id, column, new_row["value"])
        return RecordClaimResult.INSERTED_ACTIVE, True

    if diff < -delta:
        # 新が下回り: 新を superseded
        db.execute(
            f"UPDATE {claims_table} SET status='superseded' WHERE id=?",
            (new_claim_id,),
        )
        return RecordClaimResult.INSERTED_SUPERSEDED, False

    # δ 以内: 全 conflict
    kind = _conflict_kind(table, column)
    grp_id = _new_conflict_group(db, kind)
    ids = [new_claim_id] + [o["id"] for o in others]
    db.executemany(
        f"UPDATE {claims_table} SET status='conflicted', conflict_group=? WHERE id=?",
        [(grp_id, i) for i in ids],
    )
    return RecordClaimResult.INSERTED_CONFLICTED, False


def _reapply_state_machine(
    db: sqlite3.Connection,
    table: str,
    entity_id: int,
    column: str,
) -> bool:
    """(entity, column) スコープを全部 reset して §3 を貼り直す。UPSERT 異 value 用。

    canonical 更新の有無を返す。同じ規約は最高 conf 主張を採用なので、
    現実装は: 全 claim を ‘active’ に戻し、conflict_group=NULL、その後で
    最高 conf を残し他を §3 通常評価する。
    """
    claims_table = f"{table}_claims"
    fk = f"{table}_id"
    rows = db.execute(
        f"SELECT id, value, confidence FROM {claims_table} "
        f"WHERE {fk}=? AND column_name=? ORDER BY confidence DESC, created_at ASC",
        (entity_id, column),
    ).fetchall()
    if not rows:
        # 全消えなら canonical を NULL に
        old = _canonical_value(db, table, entity_id, column)
        _set_canonical(db, table, entity_id, column, None)
        return old is not None

    # まず全部 active リセット
    db.execute(
        f"UPDATE {claims_table} SET status='active', conflict_group=NULL "
        f"WHERE {fk}=? AND column_name=?",
        (entity_id, column),
    )

    # 最高 conf 行を pivot に、他行を §3 で再評価
    pivot = rows[0]
    delta = config.CLAIM_CONFIDENCE_DELTA
    conflicts: list[int] = []
    superseded: list[int] = []
    for r in rows[1:]:
        if _values_equal(r["value"], pivot["value"]):
            continue  # active のまま
        diff = pivot["confidence"] - r["confidence"]
        if diff > delta:
            superseded.append(r["id"])
        else:
            # δ 以内 → conflict
            conflicts.append(r["id"])

    if conflicts:
        conflicts = [pivot["id"]] + conflicts
        kind = _conflict_kind(table, column)
        grp_id = _new_conflict_group(db, kind)
        db.executemany(
            f"UPDATE {claims_table} SET status='conflicted', conflict_group=? WHERE id=?",
            [(grp_id, i) for i in conflicts],
        )
        # canonical は据え置き (前回値) — 但し全 conflict なら…ここでは pivot 値を残す
        old = _canonical_value(db, table, entity_id, column)
        _set_canonical(db, table, entity_id, column, pivot["value"])
        return old != pivot["value"]

    if superseded:
        db.executemany(
            f"UPDATE {claims_table} SET status='superseded' WHERE id=?",
            [(i,) for i in superseded],
        )

    old = _canonical_value(db, table, entity_id, column)
    _set_canonical(db, table, entity_id, column, pivot["value"])
    return old != pivot["value"]


def _new_conflict_group(db: sqlite3.Connection, kind: str) -> int:
    cur = db.execute(
        "INSERT INTO conflict_groups(kind, created_at) VALUES (?,?)",
        (kind, _now()),
    )
    return cur.lastrowid


# ---------- junction find_or_create ----------
def find_or_create_employment(
    db: sqlite3.Connection,
    person_id: int,
    organization_id: int,
    start_date: Optional[str],
    document_id: int,
    *,
    evidence: Optional[str] = None,
    confidence: float = 0.5,
) -> tuple[int, str]:
    """(person, org, start_date) で UNIQUE。返り値: (id, how) where how ∈ {existing, new}."""
    row = db.execute(
        "SELECT id FROM employment WHERE person_id=? AND organization_id=? "
        "AND ((start_date IS NULL AND ? IS NULL) OR start_date=?)",
        (person_id, organization_id, start_date, start_date),
    ).fetchone()
    if row:
        return row["id"], "existing"
    now = _now()
    cur = db.execute(
        "INSERT INTO employment(person_id, organization_id, start_date, document_id, "
        "evidence, confidence, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
        (person_id, organization_id, start_date, document_id, evidence, confidence, now, now),
    )
    return cur.lastrowid, "new"


def find_or_create_manufacturing(
    db: sqlite3.Connection,
    product_id: int,
    organization_id: int,
    document_id: int,
    *,
    evidence: Optional[str] = None,
    confidence: float = 0.5,
) -> tuple[int, str, Optional[int]]:
    """UNIQUE(product_id)。違反主張は staging へエスカレーション。

    戻り値: (id, how, conflict_with) where:
      how ∈ {existing, new, cardinality_violation}
      conflict_with は既存 manufacturing.id (cardinality_violation 時のみ)
    """
    row = db.execute(
        "SELECT id, organization_id FROM manufacturing WHERE product_id=?",
        (product_id,),
    ).fetchone()
    if row:
        if row["organization_id"] == organization_id:
            return row["id"], "existing", None
        return row["id"], "cardinality_violation", row["id"]
    now = _now()
    cur = db.execute(
        "INSERT INTO manufacturing(product_id, organization_id, document_id, "
        "evidence, confidence, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
        (product_id, organization_id, document_id, evidence, confidence, now, now),
    )
    return cur.lastrowid, "new", None


def find_or_create_org_hierarchy(
    db: sqlite3.Connection,
    parent_org_id: int,
    child_org_id: int,
    document_id: int,
    *,
    evidence: Optional[str] = None,
    confidence: float = 0.5,
) -> tuple[int, str, Optional[int]]:
    """UNIQUE(child_org_id)。違反主張は staging へエスカレーション。"""
    row = db.execute(
        "SELECT id, parent_org_id FROM org_hierarchy WHERE child_org_id=?",
        (child_org_id,),
    ).fetchone()
    if row:
        if row["parent_org_id"] == parent_org_id:
            return row["id"], "existing", None
        return row["id"], "cardinality_violation", row["id"]
    now = _now()
    cur = db.execute(
        "INSERT INTO org_hierarchy(parent_org_id, child_org_id, document_id, "
        "evidence, confidence, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
        (parent_org_id, child_org_id, document_id, evidence, confidence, now, now),
    )
    return cur.lastrowid, "new", None


# ---------- existence claim 状態遷移 ----------
def record_existence_claim(
    db: sqlite3.Connection,
    relation_table: str,
    junction_id: int,
    document_id: int,
    *,
    evidence: Optional[str] = None,
    confidence: float = 0.5,
) -> ClaimDispatch:
    """junction 行への存在主張を <relation>_existence_claims に記録し §3 を適用。

    junction 行の (document_id, evidence, confidence) は「最高 conf active 存在主張」
    のキャッシュとして UPDATE する。
    """
    if relation_table not in RELATION_CLAIM_COLUMNS:
        raise ValueError(f"unknown relation table: {relation_table}")
    if not (0 <= confidence <= 1):
        raise ValueError(f"confidence {confidence} out of range")

    if confidence < config.LOW_CONFIDENCE_THRESHOLD:
        return ClaimDispatch(RecordClaimResult.REJECTED_LOW_CONFIDENCE, None, False)

    claims_table = f"{relation_table}_existence_claims"
    fk = f"{relation_table}_id"

    existing = db.execute(
        f"SELECT id, confidence FROM {claims_table} WHERE {fk}=? AND document_id=?",
        (junction_id, document_id),
    ).fetchone()
    now = _now()

    if existing is not None:
        db.execute(
            f"UPDATE {claims_table} SET evidence=?, confidence=? WHERE id=?",
            (evidence, confidence, existing["id"]),
        )
        canonical_updated = _reapply_existence_state_machine(db, relation_table, junction_id)
        return ClaimDispatch(RecordClaimResult.UPDATED_EVIDENCE, existing["id"], canonical_updated)

    cur = db.execute(
        f"INSERT INTO {claims_table}({fk}, document_id, evidence, confidence, status, created_at) "
        f"VALUES (?,?,?,?,'active',?)",
        (junction_id, document_id, evidence, confidence, now),
    )
    new_claim_id = cur.lastrowid
    result, canonical_updated = _apply_existence_state_machine(
        db, relation_table, junction_id, new_claim_id,
    )
    return ClaimDispatch(result, new_claim_id, canonical_updated)


def _apply_existence_state_machine(
    db: sqlite3.Connection,
    relation_table: str,
    junction_id: int,
    new_claim_id: int,
) -> tuple[RecordClaimResult, bool]:
    """新 existence claim 挿入後の §3 判定。junction 行キャッシュを UPDATE。"""
    claims_table = f"{relation_table}_existence_claims"
    fk = f"{relation_table}_id"

    new_row = db.execute(
        f"SELECT id, confidence, document_id, evidence FROM {claims_table} WHERE id=?",
        (new_claim_id,),
    ).fetchone()
    others = db.execute(
        f"SELECT id, confidence FROM {claims_table} "
        f"WHERE {fk}=? AND id != ? AND status='active'",
        (junction_id, new_claim_id),
    ).fetchall()

    if not others:
        _update_junction_cache(db, relation_table, junction_id, new_row)
        return RecordClaimResult.INSERTED_ACTIVE, True

    # existence 主張は value がないので「同値」は常に真。multi-evidence: canonical 据え置き、
    # ただしキャッシュは最高 conf に置き換える可能性あり
    delta = config.CLAIM_CONFIDENCE_DELTA
    max_other = max(others, key=lambda o: o["confidence"])
    diff = new_row["confidence"] - max_other["confidence"]

    if diff > delta:
        # 新が上回り。既存 active は superseded
        for o in others:
            db.execute(
                f"UPDATE {claims_table} SET status='superseded' WHERE id=?",
                (o["id"],),
            )
        _update_junction_cache(db, relation_table, junction_id, new_row)
        return RecordClaimResult.INSERTED_ACTIVE, True

    if diff < -delta:
        db.execute(
            f"UPDATE {claims_table} SET status='superseded' WHERE id=?",
            (new_claim_id,),
        )
        return RecordClaimResult.INSERTED_SUPERSEDED, False

    # δ 以内 → 同 active を維持 (multi-evidence)。キャッシュは最高 conf を採用
    # ※ existence は値がないので強い意味の conflict は起きない (cardinality 違反は別経路)
    rows = others + [new_row]
    best = max(rows, key=lambda r: r["confidence"])
    if best["id"] == new_row["id"]:
        _update_junction_cache(db, relation_table, junction_id, new_row)
    return RecordClaimResult.INSERTED_ACTIVE, False


def _reapply_existence_state_machine(
    db: sqlite3.Connection,
    relation_table: str,
    junction_id: int,
) -> bool:
    """既存 existence claim の conf を UPSERT で書き換えた後の再計算。"""
    claims_table = f"{relation_table}_existence_claims"
    fk = f"{relation_table}_id"
    rows = db.execute(
        f"SELECT id, confidence, document_id, evidence FROM {claims_table} "
        f"WHERE {fk}=? ORDER BY confidence DESC, created_at ASC",
        (junction_id,),
    ).fetchall()
    if not rows:
        return False
    db.execute(
        f"UPDATE {claims_table} SET status='active', conflict_group=NULL WHERE {fk}=?",
        (junction_id,),
    )
    pivot = rows[0]
    delta = config.CLAIM_CONFIDENCE_DELTA
    superseded = [r["id"] for r in rows[1:] if pivot["confidence"] - r["confidence"] > delta]
    if superseded:
        db.executemany(
            f"UPDATE {claims_table} SET status='superseded' WHERE id=?",
            [(i,) for i in superseded],
        )
    _update_junction_cache(db, relation_table, junction_id, pivot)
    return True


def _update_junction_cache(
    db: sqlite3.Connection,
    relation_table: str,
    junction_id: int,
    claim_row,
) -> None:
    now = _now()
    db.execute(
        f"UPDATE {relation_table} SET document_id=?, evidence=?, confidence=?, updated_at=? "
        f"WHERE id=?",
        (claim_row["document_id"], claim_row["evidence"], claim_row["confidence"], now, junction_id),
    )


# ---------- relation 属性 claim ----------
def record_relation_claim(
    db: sqlite3.Connection,
    relation_table: str,
    junction_id: int,
    column: str,
    value,
    document_id: int,
    *,
    evidence: Optional[str] = None,
    confidence: float = 0.5,
) -> ClaimDispatch:
    """junction の属性列に対する claim。<relation>_claims に記録し §3 を適用。"""
    if relation_table not in RELATION_CLAIM_COLUMNS:
        raise ValueError(f"unknown relation table: {relation_table}")
    if column not in RELATION_CLAIM_COLUMNS[relation_table]:
        raise ValueError(f"{relation_table} has no claim column {column!r}")
    if not (0 <= confidence <= 1):
        raise ValueError(f"confidence {confidence} out of range")
    if confidence < config.LOW_CONFIDENCE_THRESHOLD:
        return ClaimDispatch(RecordClaimResult.REJECTED_LOW_CONFIDENCE, None, False)

    claims_table = f"{relation_table}_claims"
    fk = f"{relation_table}_id"
    now = _now()
    value_norm = _coerce_value(value)

    existing = db.execute(
        f"SELECT id, value FROM {claims_table} WHERE {fk}=? AND column_name=? AND document_id=?",
        (junction_id, column, document_id),
    ).fetchone()
    if existing is not None:
        if _values_equal(existing["value"], value_norm):
            db.execute(
                f"UPDATE {claims_table} SET evidence=?, confidence=? WHERE id=?",
                (evidence, confidence, existing["id"]),
            )
            canonical_updated = _reapply_relation_attribute_state_machine(
                db, relation_table, junction_id, column,
            )
            return ClaimDispatch(RecordClaimResult.UPDATED_EVIDENCE, existing["id"], canonical_updated)
        db.execute(
            f"UPDATE {claims_table} SET value=?, evidence=?, confidence=?, "
            f"status='active', conflict_group=NULL WHERE id=?",
            (value_norm, evidence, confidence, existing["id"]),
        )
        canonical_updated = _reapply_relation_attribute_state_machine(
            db, relation_table, junction_id, column,
        )
        return ClaimDispatch(RecordClaimResult.UPSERT_REVALUE, existing["id"], canonical_updated)

    cur = db.execute(
        f"INSERT INTO {claims_table}({fk}, column_name, value, document_id, "
        f"evidence, confidence, status, created_at) VALUES (?,?,?,?,?,?,'active',?)",
        (junction_id, column, value_norm, document_id, evidence, confidence, now),
    )
    new_claim_id = cur.lastrowid
    result, canonical_updated = _apply_relation_attribute_state_machine_for_new(
        db, relation_table, junction_id, column, new_claim_id,
    )
    return ClaimDispatch(result, new_claim_id, canonical_updated)


def _apply_relation_attribute_state_machine_for_new(
    db: sqlite3.Connection,
    relation_table: str,
    junction_id: int,
    column: str,
    new_claim_id: int,
) -> tuple[RecordClaimResult, bool]:
    claims_table = f"{relation_table}_claims"
    fk = f"{relation_table}_id"
    new_row = db.execute(
        f"SELECT id, value, confidence FROM {claims_table} WHERE id=?", (new_claim_id,)
    ).fetchone()
    others = db.execute(
        f"SELECT id, value, confidence FROM {claims_table} "
        f"WHERE {fk}=? AND column_name=? AND id != ? AND status='active'",
        (junction_id, column, new_claim_id),
    ).fetchall()
    if not others:
        _set_relation_attribute_canonical(db, relation_table, junction_id, column, new_row["value"])
        return RecordClaimResult.INSERTED_ACTIVE, True
    if any(_values_equal(o["value"], new_row["value"]) for o in others):
        return RecordClaimResult.INSERTED_ACTIVE, False
    delta = config.CLAIM_CONFIDENCE_DELTA
    max_other = max(others, key=lambda o: o["confidence"])
    diff = new_row["confidence"] - max_other["confidence"]
    if diff > delta:
        for o in others:
            db.execute(
                f"UPDATE {claims_table} SET status='superseded' WHERE id=?", (o["id"],),
            )
        _set_relation_attribute_canonical(db, relation_table, junction_id, column, new_row["value"])
        return RecordClaimResult.INSERTED_ACTIVE, True
    if diff < -delta:
        db.execute(
            f"UPDATE {claims_table} SET status='superseded' WHERE id=?", (new_claim_id,),
        )
        return RecordClaimResult.INSERTED_SUPERSEDED, False
    kind = _conflict_kind(relation_table, column)
    grp_id = _new_conflict_group(db, kind)
    ids = [new_claim_id] + [o["id"] for o in others]
    db.executemany(
        f"UPDATE {claims_table} SET status='conflicted', conflict_group=? WHERE id=?",
        [(grp_id, i) for i in ids],
    )
    return RecordClaimResult.INSERTED_CONFLICTED, False


def _reapply_relation_attribute_state_machine(
    db: sqlite3.Connection,
    relation_table: str,
    junction_id: int,
    column: str,
) -> bool:
    claims_table = f"{relation_table}_claims"
    fk = f"{relation_table}_id"
    rows = db.execute(
        f"SELECT id, value, confidence FROM {claims_table} "
        f"WHERE {fk}=? AND column_name=? ORDER BY confidence DESC, created_at ASC",
        (junction_id, column),
    ).fetchall()
    if not rows:
        old = _relation_attribute_value(db, relation_table, junction_id, column)
        _set_relation_attribute_canonical(db, relation_table, junction_id, column, None)
        return old is not None
    db.execute(
        f"UPDATE {claims_table} SET status='active', conflict_group=NULL "
        f"WHERE {fk}=? AND column_name=?",
        (junction_id, column),
    )
    pivot = rows[0]
    delta = config.CLAIM_CONFIDENCE_DELTA
    conflicts: list[int] = []
    superseded: list[int] = []
    for r in rows[1:]:
        if _values_equal(r["value"], pivot["value"]):
            continue
        if pivot["confidence"] - r["confidence"] > delta:
            superseded.append(r["id"])
        else:
            conflicts.append(r["id"])
    if conflicts:
        conflicts = [pivot["id"]] + conflicts
        kind = _conflict_kind(relation_table, column)
        grp_id = _new_conflict_group(db, kind)
        db.executemany(
            f"UPDATE {claims_table} SET status='conflicted', conflict_group=? WHERE id=?",
            [(grp_id, i) for i in conflicts],
        )
        old = _relation_attribute_value(db, relation_table, junction_id, column)
        _set_relation_attribute_canonical(db, relation_table, junction_id, column, pivot["value"])
        return old != pivot["value"]
    if superseded:
        db.executemany(
            f"UPDATE {claims_table} SET status='superseded' WHERE id=?",
            [(i,) for i in superseded],
        )
    old = _relation_attribute_value(db, relation_table, junction_id, column)
    _set_relation_attribute_canonical(db, relation_table, junction_id, column, pivot["value"])
    return old != pivot["value"]


def _relation_attribute_value(db, relation_table, junction_id, column):
    row = db.execute(
        f"SELECT {column} FROM {relation_table} WHERE id=?", (junction_id,),
    ).fetchone()
    return None if row is None else row[column]


def _set_relation_attribute_canonical(db, relation_table, junction_id, column, value):
    now = _now()
    db.execute(
        f"UPDATE {relation_table} SET {column}=?, updated_at=? WHERE id=?",
        (value, now, junction_id),
    )


# ---------- mention / weak_relations / staging ----------
def add_mention(
    db: sqlite3.Connection,
    document_id: int,
    entity_table: str,
    entity_id: int,
    surface_form: Optional[str] = None,
    span_start: Optional[int] = None,
    span_end: Optional[int] = None,
) -> int:
    if entity_table not in ENTITY_TABLES:
        raise ValueError(f"unknown entity table: {entity_table}")
    cur = db.execute(
        "INSERT INTO entity_mentions(document_id, entity_table, entity_id, surface_form, span_start, span_end) "
        "VALUES (?,?,?,?,?,?)",
        (document_id, entity_table, entity_id, surface_form, span_start, span_end),
    )
    return cur.lastrowid


def add_weak_relation(
    db: sqlite3.Connection,
    subject_table: str,
    subject_id: int,
    predicate: str,
    document_id: int,
    *,
    object_table: Optional[str] = None,
    object_id: Optional[int] = None,
    object_text: Optional[str] = None,
    evidence: Optional[str] = None,
    confidence: float = 0.3,
) -> int:
    cur = db.execute(
        "INSERT INTO weak_relations(subject_table, subject_id, predicate, object_table, "
        "object_id, object_text, document_id, evidence, confidence, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (subject_table, subject_id, predicate, object_table, object_id, object_text,
         document_id, evidence, confidence, _now()),
    )
    return cur.lastrowid


def add_staging_extraction(
    db: sqlite3.Connection,
    document_id: int,
    raw_payload: str,
    *,
    proposed_table: Optional[str] = None,
    match_summary: Optional[str] = None,
) -> int:
    cur = db.execute(
        "INSERT INTO staging_extractions(document_id, raw_payload, proposed_table, "
        "match_summary, status, created_at) VALUES (?,?,?,?, 'pending', ?)",
        (document_id, raw_payload, proposed_table, match_summary, _now()),
    )
    return cur.lastrowid


def add_schema_proposal(
    db: sqlite3.Connection,
    kind: str,
    target_table: Optional[str],
    proposed_ddl: str,
    *,
    rationale: Optional[str] = None,
    evidence_docs: Optional[str] = None,
    requires_manual_dry_run: bool = False,
) -> int:
    if kind not in ("new_table", "new_column", "rename", "split", "merge"):
        raise ValueError(f"unknown proposal kind: {kind}")
    cur = db.execute(
        "INSERT INTO schema_proposals(kind, target_table, proposed_ddl, rationale, "
        "evidence_docs, status, requires_manual_dry_run, created_at) "
        "VALUES (?,?,?,?,?, 'pending', ?, ?)",
        (kind, target_table, proposed_ddl, rationale, evidence_docs,
         1 if requires_manual_dry_run else 0, _now()),
    )
    return cur.lastrowid


# ---------- 監査クエリ ----------
def open_conflict_groups(db: sqlite3.Connection):
    return db.execute(
        "SELECT id, kind, created_at FROM conflict_groups WHERE resolved_at IS NULL "
        "ORDER BY created_at DESC"
    ).fetchall()


def pending_proposals(db: sqlite3.Connection):
    return db.execute(
        "SELECT * FROM schema_proposals WHERE status='pending' ORDER BY created_at DESC"
    ).fetchall()


def pending_staging(db: sqlite3.Connection):
    return db.execute(
        "SELECT * FROM staging_extractions WHERE status='pending' ORDER BY created_at DESC"
    ).fetchall()


def weak_relations_unpromoted(db: sqlite3.Connection):
    return db.execute(
        "SELECT * FROM weak_relations WHERE promoted_to IS NULL ORDER BY created_at DESC"
    ).fetchall()
