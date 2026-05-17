"""eval CLI エントリ。

  python -m tools.eval extract --gold data/eval/gold/extract/acme-overview.yml --runs 3
  python -m tools.eval dedup   --gold data/eval/gold/dedup/pairs.yml [--no-adjudicate]
  python -m tools.eval query   --gold data/eval/gold/query/acme-overview.yml
  python -m tools.eval all

CLI には --db / --kg-db の類のフラグは存在しない。eval は常に
data/eval/_runtime / data/eval/fixtures 配下しか触らない（fixtures.py のガード参照）。
"""
from __future__ import annotations

import argparse
from pathlib import Path

import config

from tools.eval import fixtures, io as eio


def _isolate_kg_db() -> None:
    """CLI 起動直後にプロセス全体で本番 DB から切断。restore しない。"""
    target = fixtures.fresh_runtime_db("cli-init")
    fixtures.redirect_kg_db(target)


def _add_common_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--gold", required=True, help="gold YAML へのパス")
    p.add_argument("--runs", type=int, default=1)
    p.add_argument("--tag", default="default")
    p.add_argument("--temperature", type=float, default=None, help="未指定なら config.TEMPERATURE")
    p.add_argument("--out", default=str(config.ROOT / "data" / "eval" / "runs"))


def _set_runtime_config(temperature: float | None) -> None:
    if temperature is not None:
        config.TEMPERATURE = float(temperature)


def _run_extract(args: argparse.Namespace) -> int:
    from tools.eval import extract_eval

    _set_runtime_config(args.temperature)
    gold_path = Path(args.gold).resolve()
    result = extract_eval.evaluate(gold_path, runs=args.runs)
    meta = eio.build_meta("extract", gold_path, args.tag, args.runs, args.temperature)
    out_dir = Path(args.out)
    json_path, md_path = eio.report_paths(out_dir, meta)
    eio.write_json(json_path, {
        "meta": meta.as_dict(),
        "result": result,
    })
    eio.write_markdown(
        md_path,
        meta,
        extract_eval.to_markdown_table(result),
        extract_eval.summary_text(result),
        result["per_case"],
    )
    print(f"wrote: {json_path}")
    print(f"wrote: {md_path}")
    return 0


def _run_dedup(args: argparse.Namespace) -> int:
    from tools.eval import dedup_eval

    _set_runtime_config(args.temperature)
    gold_path = Path(args.gold).resolve()
    adjudicate_enabled = not args.no_adjudicate
    result = dedup_eval.evaluate(gold_path, runs=args.runs, adjudicate_enabled=adjudicate_enabled)
    meta = eio.build_meta("dedup", gold_path, args.tag, args.runs, args.temperature)
    out_dir = Path(args.out)
    json_path, md_path = eio.report_paths(out_dir, meta)
    eio.write_json(json_path, {
        "meta": meta.as_dict(),
        "adjudicate_enabled": adjudicate_enabled,
        "result": result,
    })
    eio.write_markdown(
        md_path,
        meta,
        dedup_eval.to_markdown_table(result),
        dedup_eval.summary_text(result),
        result["per_case"],
    )
    print(f"wrote: {json_path}")
    print(f"wrote: {md_path}")
    return 0


def _run_query(args: argparse.Namespace) -> int:
    from tools.eval import query_eval

    _set_runtime_config(args.temperature)
    gold_path = Path(args.gold).resolve()
    result = query_eval.evaluate(gold_path, runs=args.runs)
    meta = eio.build_meta("query", gold_path, args.tag, args.runs, args.temperature)
    out_dir = Path(args.out)
    json_path, md_path = eio.report_paths(out_dir, meta)
    eio.write_json(json_path, {"meta": meta.as_dict(), "result": result})
    eio.write_markdown(
        md_path,
        meta,
        query_eval.to_markdown_table(result),
        query_eval.summary_text(result),
        result["per_case"],
    )
    print(f"wrote: {json_path}")
    print(f"wrote: {md_path}")
    return 0


def _run_all(args: argparse.Namespace) -> int:
    raise SystemExit("`all` サブコマンドは Phase 4 で実装予定です")


def main() -> None:
    _isolate_kg_db()
    parser = argparse.ArgumentParser(prog="tools.eval", description="精度評価ランナー")
    sub = parser.add_subparsers(dest="stage", required=True)

    p_ex = sub.add_parser("extract", help="Graph 抽出 evaluator")
    _add_common_args(p_ex)
    p_ex.set_defaults(func=_run_extract)

    p_de = sub.add_parser("dedup", help="Entity dedup evaluator (Phase 2)")
    _add_common_args(p_de)
    p_de.add_argument("--no-adjudicate", action="store_true", help="SLM 同一判定を無効化（規則のみ）")
    p_de.set_defaults(func=_run_dedup)

    p_q = sub.add_parser("query", help="Query evaluator (Phase 3)")
    _add_common_args(p_q)
    p_q.set_defaults(func=_run_query)

    p_all = sub.add_parser("all", help="all stages (Phase 4)")
    _add_common_args(p_all)
    p_all.set_defaults(func=_run_all)

    args = parser.parse_args()
    raise SystemExit(args.func(args))
