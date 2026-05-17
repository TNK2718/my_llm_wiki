"""Stage 2: Entity Dedup evaluator。

ペア毎にテンポラリ SQLite ファイルを作って独立性を担保。本番 DB には触れない
（fixtures.redirect_kg_db / assert_not_prod を通す）。
"""
from __future__ import annotations

from pathlib import Path

import config
import db as kg
import ingest

from tools.eval import fixtures
from tools.eval import io as eio


VALID_LEVELS = {"exact", "alias", "alias-or-llm", "llm-merge"}


def _level_satisfied(expected_level: str | None, actual_how: str) -> bool:
    """期待 level と実際の how の整合判定。
    - exact:        actual は 'exact' のみ
    - alias-or-llm: actual は 'alias' / 'llm-merge' のどちらでも OK
    - llm-merge:    actual は 'llm-merge' のみ
    - 未指定:       same と判定されさえすれば OK
    """
    if not expected_level:
        return actual_how != "new"
    if expected_level == "exact":
        return actual_how == "exact"
    if expected_level == "alias-or-llm":
        return actual_how in ("alias", "llm-merge")
    if expected_level == "llm-merge":
        return actual_how == "llm-merge"
    if expected_level == "alias":
        return actual_how == "alias"
    return False


def _eval_pair(pair: dict, adjudicate) -> dict:
    """1 ペアを独立した temp DB で評価。adjudicate=None なら SLM 無効。"""
    # ペア毎に新しい DB を作って redirect。前ペアの状態を持ち越さない。
    target = fixtures.fresh_runtime_db("dedup")
    fixtures.redirect_kg_db(target)
    db = kg.connect()
    try:
        a, b = pair["a"], pair["b"]
        etype = pair.get("type", "concept")
        eid_a, how_a = kg.find_or_stage_entity(db, a, etype, adjudicate=adjudicate)
        eid_b, how_b = kg.find_or_stage_entity(db, b, etype, adjudicate=adjudicate)
        predicted = "same" if eid_a == eid_b else "different"
        expected = pair["expected"]
        expected_level = pair.get("level")
        return {
            "a": a,
            "b": b,
            "type": etype,
            "expected": expected,
            "predicted": predicted,
            "correct": expected == predicted,
            "how_a": how_a,
            "how_b": how_b,
            "expected_level": expected_level,
            "level_match": _level_satisfied(expected_level, how_b) if expected == "same" else None,
        }
    finally:
        db.close()
        try:
            target.unlink(missing_ok=True)
        except OSError:
            pass


def evaluate(gold_path: Path, runs: int, adjudicate_enabled: bool) -> dict:
    gold = eio.load_yaml(gold_path)
    pairs = gold.get("pairs") or []

    # level の妥当性検証（spec 違反を早期に弾く）
    for p in pairs:
        lv = p.get("level")
        if lv is not None and lv not in VALID_LEVELS:
            raise ValueError(f"unknown level={lv!r} in pair {p}")

    adjudicate = ingest.make_adjudicator() if adjudicate_enabled else None

    per_run: list[dict] = []
    last_per_case: list[dict] = []
    for i in range(runs):
        results = [_eval_pair(p, adjudicate) for p in pairs]

        tp = sum(1 for r in results if r["expected"] == "same" and r["predicted"] == "same")
        fn = sum(1 for r in results if r["expected"] == "same" and r["predicted"] == "different")
        fp = sum(1 for r in results if r["expected"] == "different" and r["predicted"] == "same")
        tn = sum(1 for r in results if r["expected"] == "different" and r["predicted"] == "different")

        same_total = tp + fn
        diff_total = tn + fp
        n = same_total + diff_total

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
            "run": i + 1,
            "n_pairs": n,
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
            "adjudicate_enabled": adjudicate_enabled,
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
        f"gold: pairs={gs['n_pairs']} (same={gs['n_same']}, different={gs['n_different']}), "
        f"adjudicate={gs['adjudicate_enabled']}; last-run how_b counts: {how}"
    )
