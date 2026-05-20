"""Phase 4: schema_proposals / staging / weak_relations の品質評価。

4 メトリクス (docs/typed-schema-design.md「eval gold への影響」):
- schema_proposal_precision : 期待された (kind, target_table) が生成されたか (PRF)
- staging_replay_correctness : 期待された (reason, proposed_table) が staging に積まれたか (PRF)
- canonical_stability       : 同 doc を N 回 ingest した時の canonical UPDATE 回数
- weak_relation_promotion_recall : 期待された predicate が promoted_to にマークされたか

evaluate() は LLM-driven ingest を回す。compute_metrics() は DB 状態を受け取って
純関数として PRF / 各メトリクスを算出するので、ユニットテストはこちらを叩く。
"""
from __future__ import annotations

import importlib
import json
import sqlite3
from pathlib import Path
from typing import Any

import config

from tools.eval import fixtures, io as eio
from tools.eval.metrics import PRF, aggregate


# ---------- 純関数: DB 状態 → メトリクス ----------

def _gold_proposal_set(gold: dict) -> set[tuple[str, str]]:
    out = set()
    for p in gold.get("expected_proposals") or []:
        out.add((p["kind"], p["target_table"]))
    return out


def _pred_proposal_set(db: sqlite3.Connection) -> set[tuple[str, str]]:
    out = set()
    for r in db.execute(
        "SELECT kind, target_table FROM schema_proposals WHERE target_table IS NOT NULL"
    ):
        out.add((r["kind"], r["target_table"]))
    return out


def _gold_staging_set(gold: dict) -> set[tuple[str, str]]:
    out = set()
    for s in gold.get("expected_staging") or []:
        out.add((s["reason"], s.get("proposed_table") or ""))
    return out


def _pred_staging_set(db: sqlite3.Connection) -> set[tuple[str, str]]:
    out = set()
    for r in db.execute(
        "SELECT proposed_table, match_summary FROM staging_extractions"
    ):
        try:
            m = json.loads(r["match_summary"] or "{}")
        except json.JSONDecodeError:
            continue
        reason = m.get("reason") or ""
        out.add((reason, r["proposed_table"] or ""))
    return out


def _gold_promotion_set(gold: dict) -> set[str]:
    return {p["predicate"] for p in gold.get("expected_weak_promotion") or []}


def _pred_promotion_set(db: sqlite3.Connection) -> set[str]:
    return {
        r["predicate"]
        for r in db.execute(
            "SELECT DISTINCT predicate FROM weak_relations WHERE promoted_to IS NOT NULL"
        )
    }


def _count_canonical_updates(db: sqlite3.Connection) -> dict[tuple[str, int, str], int]:
    """各 (entity_table, id, column) の active claim 数を canonical 反映回数の上限として返す。

    厳密な UPDATE 回数は claims の created_at 経過で推定する。superseded 含む全数 - 1 を
    「最低限の flip 回数」とみなす（同値多文書を区別しない簡易版）。
    """
    counts: dict[tuple[str, int, str], int] = {}
    for table in ("person", "organization", "product", "project"):
        rows = db.execute(
            f"SELECT {table}_id AS eid, column_name, "
            f"       COUNT(DISTINCT value) AS distinct_vals "
            f"FROM {table}_claims GROUP BY {table}_id, column_name"
        ).fetchall()
        for r in rows:
            n = max(0, (r["distinct_vals"] or 1) - 1)
            counts[(table, r["eid"], r["column_name"])] = n
    return counts


def compute_metrics(db: sqlite3.Connection, gold: dict) -> dict:
    """DB 状態と gold から 4 メトリクスを計算（純関数）。"""
    # 1. schema_proposal_precision
    gs_p = _gold_proposal_set(gold)
    ps_p = _pred_proposal_set(db)
    prf_p = PRF(len(gs_p & ps_p), len(ps_p - gs_p), len(gs_p - ps_p)).as_dict()

    # 2. staging_replay_correctness
    gs_s = _gold_staging_set(gold)
    ps_s = _pred_staging_set(db)
    prf_s = PRF(len(gs_s & ps_s), len(ps_s - gs_s), len(gs_s - ps_s)).as_dict()

    # 3. canonical_stability
    flips = _count_canonical_updates(db)
    max_allowed = int((gold.get("canonical_stability") or {}).get("max_flips_per_entity_column", 0))
    worst = max(flips.values()) if flips else 0
    stability_pass = worst <= max_allowed

    # 4. weak_relation_promotion_recall
    gs_w = _gold_promotion_set(gold)
    ps_w = _pred_promotion_set(db)
    prf_w = PRF(len(gs_w & ps_w), len(ps_w - gs_w), len(gs_w - ps_w)).as_dict()

    return {
        "schema_proposal_precision": prf_p,
        "staging_replay_correctness": prf_s,
        "canonical_stability": {
            "max_flips_observed": worst,
            "max_flips_allowed": max_allowed,
            "passed": stability_pass,
            "per_entity": [
                {"table": k[0], "entity_id": k[1], "column": k[2], "flips": v}
                for k, v in sorted(flips.items()) if v > 0
            ],
        },
        "weak_relation_promotion_recall": prf_w,
        "predicted": {
            "proposals": sorted(ps_p),
            "staging": sorted(ps_s),
            "promoted": sorted(ps_w),
        },
        "expected": {
            "proposals": sorted(gs_p),
            "staging": sorted(gs_s),
            "promoted": sorted(gs_w),
        },
    }


# ---------- 高レベル runner: LLM 経由 ingest ----------

def _ingest_into_runtime(slug: str, source_path: Path, runs: int) -> Path:
    """新規 runtime DB を発行し、source を runs 回 ingest して DB パスを返す。

    runs > 1 は canonical_stability の計測用 (同 doc を繰り返し ingest)。
    """
    target = fixtures.fresh_runtime_db(f"proposal-{slug}")
    fixtures.redirect_kg_db(target)

    # ingest 側は import 時に config.KG_DB を解決していないのでそのまま使える。
    # db モジュールは config.KG_DB を都度参照する設計なので reload 不要。
    import ingest

    rel = source_path.relative_to(config.ROOT) if source_path.is_absolute() else source_path
    for _ in range(max(1, runs)):
        ingest.ingest_file(str(rel))

    return target


def evaluate(gold_path: Path, runs: int = 1) -> dict:
    """LLM 経由 ingest + メトリクス計算。runs 回 ingest で canonical_stability を測る。"""
    gold = eio.load_yaml(gold_path)
    slug = gold.get("doc_slug") or "unknown"
    source = (config.ROOT / gold["source"]).resolve()

    db_path = _ingest_into_runtime(slug, source, runs)

    # ingest 後に weak の auto-promotion を 1 度走らせる (ingest 内で実行済みだが冪等)
    import db as kg
    conn = kg.connect()
    try:
        with kg.writer(conn):
            import ingest
            ingest._auto_promote_weak_predicates(conn)
        metrics = compute_metrics(conn, gold)
    finally:
        conn.close()

    return {
        "gold_summary": {
            "doc_slug": slug,
            "n_expected_proposals": len(gold.get("expected_proposals") or []),
            "n_expected_staging": len(gold.get("expected_staging") or []),
            "n_expected_promotion": len(gold.get("expected_weak_promotion") or []),
        },
        "runtime_db": str(db_path),
        "runs": runs,
        "per_run": [metrics],
        "aggregate": _aggregate_one(metrics),
        "per_case": _failures(metrics),
    }


def _aggregate_one(m: dict) -> dict:
    flat = {
        "proposal_precision": m["schema_proposal_precision"]["precision"],
        "proposal_recall":    m["schema_proposal_precision"]["recall"],
        "proposal_f1":        m["schema_proposal_precision"]["f1"],
        "staging_precision":  m["staging_replay_correctness"]["precision"],
        "staging_recall":     m["staging_replay_correctness"]["recall"],
        "staging_f1":         m["staging_replay_correctness"]["f1"],
        "weak_promotion_precision": m["weak_relation_promotion_recall"]["precision"],
        "weak_promotion_recall":    m["weak_relation_promotion_recall"]["recall"],
        "canonical_stability_passed": 1.0 if m["canonical_stability"]["passed"] else 0.0,
    }
    return aggregate([flat], list(flat.keys()))


def _failures(m: dict) -> list[dict]:
    out: list[dict] = []
    pred = m["predicted"]
    exp = m["expected"]
    for missing in set(map(tuple, exp["proposals"])) - set(map(tuple, pred["proposals"])):
        out.append({"label": "proposal missed", "kind": missing[0], "target_table": missing[1]})
    for extra in set(map(tuple, pred["proposals"])) - set(map(tuple, exp["proposals"])):
        out.append({"label": "proposal extra",  "kind": extra[0],   "target_table": extra[1]})
    for missing in set(map(tuple, exp["staging"])) - set(map(tuple, pred["staging"])):
        out.append({"label": "staging missed", "reason": missing[0], "proposed_table": missing[1]})
    for extra in set(map(tuple, pred["staging"])) - set(map(tuple, exp["staging"])):
        out.append({"label": "staging extra",  "reason": extra[0],   "proposed_table": extra[1]})
    for missing in set(exp["promoted"]) - set(pred["promoted"]):
        out.append({"label": "promotion missed", "predicate": missing})
    if not m["canonical_stability"]["passed"]:
        out.append({
            "label": "canonical_stability failed",
            "max_flips_observed": m["canonical_stability"]["max_flips_observed"],
            "max_flips_allowed": m["canonical_stability"]["max_flips_allowed"],
        })
    return out


def to_markdown_table(result: dict) -> list[list[Any]]:
    a = result["aggregate"]
    rows = [["metric", "value"]]
    for k in (
        "proposal_precision_mean", "proposal_recall_mean", "proposal_f1_mean",
        "staging_precision_mean", "staging_recall_mean", "staging_f1_mean",
        "weak_promotion_precision_mean", "weak_promotion_recall_mean",
        "canonical_stability_passed_mean",
    ):
        # aggregate() は None を弾くので、欠落 = 採点対象なし = "n/a"
        if k in a:
            rows.append([k.removesuffix("_mean"), f"{a[k]:.3f}"])
        else:
            rows.append([k.removesuffix("_mean"), "n/a"])
    return rows


def summary_text(result: dict) -> str:
    gs = result["gold_summary"]
    return (
        f"gold: doc={gs['doc_slug']}, "
        f"expected proposals={gs['n_expected_proposals']}, "
        f"staging={gs['n_expected_staging']}, "
        f"promotion={gs['n_expected_promotion']}"
    )
