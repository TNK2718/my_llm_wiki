"""Stage 1: Graph 抽出 evaluator。DB に一切触らない（kg を import しない）。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import config
import ingest
import llm

from tools.eval import io as eio
from tools.eval import matchers
from tools.eval.metrics import PRF, aggregate


def _run_once(body: str):
    """typed extract: returns GraphExtraction with entities/relations/weak_relations."""
    chunks = ingest.semantic_chunk(body, config.CHUNK_CHARS)
    g = ingest.extract_graph_from_chunks(chunks)
    ents = [e.model_dump(by_alias=True) for e in g.entities]
    rels = [r.model_dump(by_alias=True) for r in g.relations]
    weak = [w.model_dump() for w in g.weak_relations]
    return ents, rels, weak


def evaluate(gold_path: Path, runs: int) -> dict:
    gold = eio.load_yaml(gold_path)
    source = (config.ROOT / gold["source"]).resolve()
    body = source.read_text(encoding="utf-8", errors="replace")

    g_ent = gold.get("entities") or []
    g_rel = gold.get("relations") or []
    g_fact = gold.get("entities") or []  # attribute claims live inside entities now

    per_run: list[dict] = []
    last_per_case: list[dict] = []
    for i in range(runs):
        with llm.trace_session() as trace:
            ents, rels, _weak = _run_once(body)
        m_e = matchers.match_entities(g_ent, ents)
        m_r = matchers.match_relations(g_rel, rels)
        m_f = matchers.match_facts(g_fact, ents)

        prf_e = PRF(m_e["tp"], m_e["fp"], m_e["fn"]).as_dict()
        prf_r = PRF(m_r["tp"], m_r["fp"], m_r["fn"]).as_dict()
        prf_f = PRF(m_f["tp"], m_f["fp"], m_f["fn"]).as_dict()

        per_run.append({
            "run": i + 1,
            "entities_precision": prf_e["precision"],
            "entities_recall": prf_e["recall"],
            "entities_f1": prf_e["f1"],
            "relations_precision": prf_r["precision"],
            "relations_recall": prf_r["recall"],
            "relations_f1": prf_r["f1"],
            "facts_precision": prf_f["precision"],
            "facts_recall": prf_f["recall"],
            "facts_f1": prf_f["f1"],
            "pred_counts": {"entities": len(ents), "relations": len(rels), "facts": len(facts)},
            "trace": trace,
        })

        last_per_case = []
        for cat, m in [("entities", m_e), ("relations", m_r), ("facts", m_f)]:
            for item in m["missed"]:
                last_per_case.append({"label": f"{cat} missed (run {i + 1})", "category": cat, "kind": "missed", **item})
            for item in m["extra"]:
                last_per_case.append({"label": f"{cat} extra (run {i + 1})", "category": cat, "kind": "extra", **item})

    keys = [
        "entities_precision", "entities_recall", "entities_f1",
        "relations_precision", "relations_recall", "relations_f1",
        "facts_precision", "facts_recall", "facts_f1",
    ]
    agg = aggregate(per_run, keys)

    return {
        "gold_summary": {
            "doc_slug": gold.get("doc_slug"),
            "n_entities": len(g_ent),
            "n_relations": len(g_rel),
            "n_attribute_claims": sum(len((e.get("attributes") or {})) for e in g_fact),
        },
        "per_run": per_run,
        "aggregate": agg,
        "per_case": last_per_case,
    }


def to_markdown_table(result: dict) -> list[list[Any]]:
    """metrics 表 (firstrow が header)。"""
    a = result["aggregate"]
    rows = [["category", "precision (mean±std)", "recall (mean±std)", "f1 (mean±std)"]]
    for cat in ("entities", "relations", "facts"):
        rows.append([
            cat,
            f"{a.get(f'{cat}_precision_mean', 0):.3f} ± {a.get(f'{cat}_precision_std', 0):.3f}",
            f"{a.get(f'{cat}_recall_mean', 0):.3f} ± {a.get(f'{cat}_recall_std', 0):.3f}",
            f"{a.get(f'{cat}_f1_mean', 0):.3f} ± {a.get(f'{cat}_f1_std', 0):.3f}",
        ])
    return rows


def summary_text(result: dict) -> str:
    gs = result["gold_summary"]
    return (
        f"gold: doc={gs['doc_slug']}, "
        f"entities={gs['n_entities']}, relations={gs['n_relations']}, facts={gs['n_facts']}"
    )
