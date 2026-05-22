"""Stage 3: Query evaluator。

fixture (data/eval/fixtures/<name>.sqlite) を config.KG_DB に redirect してから
query.answer_question() を呼ぶ。本番 DB には決して触れない。
"""
from __future__ import annotations

from pathlib import Path

import config
import db as kg
import llm

from tools.eval import fixtures
from tools.eval import io as eio
from tools.eval.metrics import aggregate


def _norm(s) -> str:
    return kg.normalize(str(s) if s is not None else "")


def _row_contains_match(rows: list[dict], spec: dict) -> bool:
    """expected_row_contains の 1 エントリが rows の任意の行に出現するか。

    column を省略するか "*" にすると全列スキャン (SLM 生成 SQL の列名は
    予測困難なため、値だけで判定したい場合に使う)。
    """
    col = spec.get("column")
    expected_value = spec.get("value")
    if expected_value is None:
        return False
    target = _norm(expected_value)
    if not target:
        return False
    wildcard = (col is None) or (col == "*")
    for r in rows or []:
        cells = r.values() if wildcard else [r.get(col)]
        for cell in cells:
            if cell is None:
                continue
            if target in _norm(cell):
                return True
    return False


def _evaluate_one(qa: dict, answer_question) -> dict:
    q = qa["q"]
    expected_contains = qa.get("expected_row_contains") or []
    expected_slugs = qa.get("expected_doc_slugs") or []

    with llm.trace_session() as trace:
        res = answer_question(q)
    rows = res.get("rows") or []
    docs = res.get("docs") or []
    sql = res.get("sql")
    error = res.get("error")

    # row contains
    contains_hits = [spec for spec in expected_contains if _row_contains_match(rows, spec)]
    contains_total = len(expected_contains)
    contains_rate = (len(contains_hits) / contains_total) if contains_total else None

    # doc slugs: FTS と SQL 経路 (sql_docs) の union を取る
    sql_docs_ = res.get("sql_docs") or []
    pred_slugs = sorted(
        {d.get("slug") for d in docs} | {d.get("slug") for d in sql_docs_}
    )
    pred_slugs = [s for s in pred_slugs if s]
    slug_hits = [s for s in expected_slugs if s in pred_slugs]
    slug_rate = (len(slug_hits) / len(expected_slugs)) if expected_slugs else None

    # SQL 実行成功: sql が生成されかつ text2sql 例外 fallback でないこと。
    sql_ok = bool(sql) and (error is None or "text2sql 失敗" not in (error or ""))

    return {
        "q": q,
        "sql": sql,
        "sql_ok": sql_ok,
        "n_rows": len(rows),
        "row_contains_rate": contains_rate,
        "doc_slug_rate": slug_rate,
        "error": error,
        "missed_contains": [spec for spec in expected_contains if spec not in contains_hits],
        "missed_slugs": [s for s in expected_slugs if s not in slug_hits],
        "trace": trace,
    }


def _run_once(qa_list: list[dict]) -> dict:
    # 遅延 import: fixture redirect 後に config.KG_DB を参照する
    import query as q_mod

    results = [_evaluate_one(qa, q_mod.answer_question) for qa in qa_list]
    n = len(results)
    sql_ok_count = sum(1 for r in results if r["sql_ok"])
    contains_eligible = [r for r in results if r["row_contains_rate"] is not None]
    contains_avg = (
        sum(r["row_contains_rate"] for r in contains_eligible) / len(contains_eligible)
        if contains_eligible
        else 0.0
    )
    slug_eligible = [r for r in results if r["doc_slug_rate"] is not None]
    slug_avg = (
        sum(r["doc_slug_rate"] for r in slug_eligible) / len(slug_eligible)
        if slug_eligible
        else 0.0
    )

    return {
        "results": results,
        "sql_success_rate": round(sql_ok_count / n, 4) if n else 0.0,
        "row_contains_rate": round(contains_avg, 4),
        "doc_slug_rate": round(slug_avg, 4),
    }


def evaluate(gold_path: Path, runs: int) -> dict:
    gold = eio.load_yaml(gold_path)
    fixture_name = gold.get("fixture")
    if not fixture_name:
        raise ValueError("query gold には fixture: <name>.sqlite が必須")
    fixture_path = fixtures.FIXTURES_DIR / fixture_name
    extract_gold = gold.get("extract_gold")
    if not fixture_path.exists() and extract_gold:
        fixtures.build_fixture(config.ROOT / extract_gold, fixture_path)
    if not fixture_path.exists():
        raise FileNotFoundError(
            f"fixture が見つからず再生成もできません: {fixture_path}\n"
            "gold に extract_gold を指定するか、事前に fixtures.build_fixture() を実行してください。"
        )

    # 本番 DB から確実に切断（CLI 起動時にも redirect 済みだが念のため fixture に再 redirect）
    fixtures.redirect_kg_db(fixture_path)

    qa_list = gold.get("qa") or []
    per_run: list[dict] = []
    last_per_case: list[dict] = []
    for i in range(runs):
        run = _run_once(qa_list)
        per_run.append({
            "run": i + 1,
            "sql_success_rate": run["sql_success_rate"],
            "row_contains_rate": run["row_contains_rate"],
            "doc_slug_rate": run["doc_slug_rate"],
        })
        last_per_case = []
        for r in run["results"]:
            ok = (
                r["sql_ok"]
                and (r["row_contains_rate"] in (None, 1.0))
                and (r["doc_slug_rate"] in (None, 1.0))
            )
            if not ok:
                last_per_case.append({
                    "label": f"q: {r['q']}",
                    "row_contains_rate": r["row_contains_rate"],
                    "doc_slug_rate": r["doc_slug_rate"],
                    "missed_contains": r["missed_contains"],
                    "missed_slugs": r["missed_slugs"],
                    "sql": r["sql"],
                    "sql_ok": r["sql_ok"],
                    "n_rows": r["n_rows"],
                    "error": r["error"],
                    "trace": r.get("trace") or [],
                })

    agg = aggregate(per_run, ["sql_success_rate", "row_contains_rate", "doc_slug_rate"])
    return {
        "gold_summary": {
            "n_qa": len(qa_list),
            "fixture": str(fixture_path),
        },
        "per_run": per_run,
        "aggregate": agg,
        "per_case": last_per_case,
    }


def to_markdown_table(result: dict) -> list[list]:
    a = result["aggregate"]
    return [
        ["metric", "mean ± std"],
        ["sql_success_rate",  f"{a.get('sql_success_rate_mean', 0):.3f} ± {a.get('sql_success_rate_std', 0):.3f}"],
        ["row_contains_rate", f"{a.get('row_contains_rate_mean', 0):.3f} ± {a.get('row_contains_rate_std', 0):.3f}"],
        ["doc_slug_rate",     f"{a.get('doc_slug_rate_mean', 0):.3f} ± {a.get('doc_slug_rate_std', 0):.3f}"],
    ]


def summary_text(result: dict) -> str:
    gs = result["gold_summary"]
    return f"gold: qa={gs['n_qa']}, fixture={gs['fixture']}"
