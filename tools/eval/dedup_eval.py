"""Stage 2: Entity Dedup evaluator (typed-schema)。

各ペアを独立した temp DB で評価。本番 DB には触れない。
typed pipeline では adjudicate (LLM) ステップは存在しないので、突合経路は
exact (norm_key 一致) / alias (alias 表一致) / new (突合不能) の 3 種のみ。
"""
from __future__ import annotations

from pathlib import Path

import config
import db as kg
import llm

from tools.eval import fixtures
from tools.eval import io as eio


VALID_LEVELS = {"exact", "alias", "alias-or-llm", "llm-merge"}


def _level_satisfied(expected_level: str | None, actual_how: str) -> bool:
    """typed pipeline は LLM 突合経路を持たないので、'alias-or-llm' と 'llm-merge'
    は実質 'new' で失敗扱いになる。期待 level vs 実際 how の整合:
    """
    if not expected_level:
        return actual_how != "new"
    if expected_level == "exact":
        return actual_how == "exact"
    if expected_level == "alias":
        return actual_how == "alias"
    if expected_level == "alias-or-llm":
        return actual_how in ("alias",)  # typed には llm 経路なし
    if expected_level == "llm-merge":
        return False  # typed では実現不能
    return False


def _bootstrap_doc(db) -> int:
    return kg.upsert_document(db, "dedup_eval", "dedup eval doc", None, "")


def _eval_pair(pair: dict) -> dict:
    """1 ペアを独立した temp DB で評価。"""
    target = fixtures.fresh_runtime_db("dedup")
    fixtures.redirect_kg_db(target)
    db = kg.connect()
    try:
        a, b = pair["a"], pair["b"]
        table = pair.get("table") or pair.get("type") or "person"
        if table not in kg.ENTITY_TABLES:
            # 旧 gold (type=org/concept) の保険
            table = {"org": "organization"}.get(table, "person")
        with llm.trace_session() as trace:
            with kg.writer(db):
                _bootstrap_doc(db)
                llm.note("pair", a=a, b=b, table=table)
                eid_a, how_a = kg.upsert_entity(db, table, a)
                llm.note("stage.a", entity_id=eid_a, how=how_a)
                # b は a と同じ entity に着地するべきか? alias 経路を通すために
                # 後ろから明示的に追加するのではなく、単に upsert_entity を再呼びすると
                # 同じ norm_key で見つかれば 'exact', alias 表に b が登録済なら 'alias'。
                # b が独立 norm_key を持てば 'new'。
                eid_b, how_b = kg.upsert_entity(db, table, b)
                llm.note("stage.b", entity_id=eid_b, how=how_b)
        predicted = "same" if eid_a == eid_b else "different"
        expected = pair["expected"]
        expected_level = pair.get("level")
        return {
            "a": a, "b": b, "table": table,
            "expected": expected, "predicted": predicted,
            "correct": expected == predicted,
            "how_a": how_a, "how_b": how_b,
            "expected_level": expected_level,
            "level_match": _level_satisfied(expected_level, how_b) if expected == "same" else None,
            "trace": trace,
        }
    finally:
        db.close()
        try:
            target.unlink(missing_ok=True)
        except OSError:
            pass


def evaluate(gold_path: Path, runs: int, adjudicate_enabled: bool = False) -> dict:
    """adjudicate_enabled は API 互換のため残すが、typed pipeline では無視される."""
    gold = eio.load_yaml(gold_path)
    pairs = gold.get("pairs") or []

    for p in pairs:
        lv = p.get("level")
        if lv is not None and lv not in VALID_LEVELS:
            raise ValueError(f"unknown level={lv!r} in pair {p}")

    per_run: list[dict] = []
    last_per_case: list[dict] = []
    for i in range(runs):
        results = [_eval_pair(p) for p in pairs]

        tp = sum(1 for r in results if r["expected"] == "same" and r["predicted"] == "same")
        fn = sum(1 for r in results if r["expected"] == "same" and r["predicted"] == "different")
        fp = sum(1 for r in results if r["expected"] == "different" and r["predicted"] == "same")
        tn = sum(1 for r in results if r["expected"] == "different" and r["predicted"] == "different")
        n = tp + fn + tn + fp
        precision = tp / (tp + fp) if (tp + fp) else 0.0
        recall = tp / (tp + fn) if (tp + fn) else 0.0
        f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
        accuracy = (tp + tn) / n if n else 0.0
        same_cases = [r for r in results if r["expected"] == "same"]
        level_match_n = sum(1 for r in same_cases if r.get("level_match") is True)
        level_match_rate = (level_match_n / len(same_cases)) if same_cases else 0.0

        how_counter: dict[str, int] = {}
        for r in results:
            how_counter[r["how_b"]] = how_counter.get(r["how_b"], 0) + 1

        per_run.append({
            "run": i + 1, "n_pairs": n,
            "tp": tp, "fp": fp, "tn": tn, "fn": fn,
            "accuracy": round(accuracy, 4),
            "precision": round(precision, 4),
            "recall": round(recall, 4),
            "f1": round(f1, 4),
            "level_match_rate": round(level_match_rate, 4),
            "how_counts": how_counter,
        })
        last_per_case = [r for r in results if not r["correct"] or (r.get("level_match") is False)]
        for r in last_per_case:
            r["label"] = (
                f"{'wrong' if not r['correct'] else 'level-mismatch'}: "
                f"{r['a']} ↔ {r['b']} (expected={r['expected']}/{r['expected_level']}, "
                f"got={r['predicted']}/{r['how_b']})"
            )

    from tools.eval.metrics import aggregate
    agg = aggregate(per_run, ["accuracy", "precision", "recall", "f1", "level_match_rate"])

    return {
        "gold_summary": {
            "n_pairs": len(pairs),
            "n_same": sum(1 for p in pairs if p["expected"] == "same"),
            "n_different": sum(1 for p in pairs if p["expected"] == "different"),
            "adjudicate_enabled": False,  # typed pipeline では常に false
        },
        "per_run": per_run,
        "aggregate": agg,
        "per_case": last_per_case,
    }


def to_markdown_table(result: dict) -> list[list]:
    a = result["aggregate"]
    return [
        ["metric", "mean ± std"],
        ["accuracy",         f"{a.get('accuracy_mean', 0):.3f} ± {a.get('accuracy_std', 0):.3f}"],
        ["precision (same)", f"{a.get('precision_mean', 0):.3f} ± {a.get('precision_std', 0):.3f}"],
        ["recall (same)",    f"{a.get('recall_mean', 0):.3f} ± {a.get('recall_std', 0):.3f}"],
        ["f1 (same)",        f"{a.get('f1_mean', 0):.3f} ± {a.get('f1_std', 0):.3f}"],
        ["level_match_rate", f"{a.get('level_match_rate_mean', 0):.3f} ± {a.get('level_match_rate_std', 0):.3f}"],
    ]


def summary_text(result: dict) -> str:
    gs = result["gold_summary"]
    how = result["per_run"][-1]["how_counts"] if result["per_run"] else {}
    return (
        f"gold: pairs={gs['n_pairs']} (same={gs['n_same']}, different={gs['n_different']}); "
        f"last-run how_b counts: {how}"
    )
