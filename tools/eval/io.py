"""YAML 読込・レポート書き出し・run metadata。"""
from __future__ import annotations

import json
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from tabulate import tabulate

import config


@dataclass
class RunMeta:
    timestamp_utc: str
    model: str
    temperature: float
    num_ctx: int
    git_rev: str
    tag: str
    runs: int
    stage: str
    gold_path: str

    def as_dict(self) -> dict:
        return asdict(self)


def utc_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")


def git_rev() -> str:
    try:
        return (
            subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=str(config.ROOT), stderr=subprocess.DEVNULL
            )
            .decode()
            .strip()
        )
    except Exception:
        return "unknown"


def build_meta(stage: str, gold_path: Path | str, tag: str, runs: int, temperature: float | None) -> RunMeta:
    return RunMeta(
        timestamp_utc=utc_ts(),
        model=config.MODEL,
        temperature=config.TEMPERATURE if temperature is None else float(temperature),
        num_ctx=config.NUM_CTX,
        git_rev=git_rev(),
        tag=tag,
        runs=runs,
        stage=stage,
        gold_path=str(gold_path),
    )


def load_yaml(path: Path | str) -> dict:
    p = Path(path)
    with p.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError(f"gold YAML はオブジェクトを期待: {p}")
    return data


def report_paths(out_dir: Path, meta: RunMeta) -> tuple[Path, Path]:
    safe_model = meta.model.replace(":", "-").replace("/", "-")
    safe_tag = meta.tag.replace(" ", "-")
    base = f"{meta.timestamp_utc}__{safe_model}__{safe_tag}__{meta.stage}"
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir / f"{base}.json", out_dir / f"{base}.md"


def write_json(path: Path, payload: dict) -> None:
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def write_markdown(path: Path, meta: RunMeta, metrics_table: list[list], summary: str, failures: list[dict]) -> None:
    lines = []
    lines.append(f"# eval report — {meta.stage}\n")
    lines.append("## run metadata\n")
    lines.append(_kv_table(meta.as_dict()) + "\n")
    lines.append("## metrics\n")
    lines.append(tabulate(metrics_table, headers="firstrow", tablefmt="github") + "\n")
    if summary:
        lines.append("\n" + summary + "\n")
    if failures:
        lines.append("\n## failures (top 20)\n")
        for i, fail in enumerate(failures[:20], 1):
            lines.append(f"### {i}. {fail.get('label', '')}\n")
            lines.append("```\n" + json.dumps(fail, ensure_ascii=False, indent=2) + "\n```\n")
    path.write_text("\n".join(lines), encoding="utf-8")


def _kv_table(d: dict[str, Any]) -> str:
    rows = [[k, v] for k, v in d.items()]
    return tabulate(rows, headers=["key", "value"], tablefmt="github")
