"""精度メトリクス。集合一致ベースの P/R/F1 と、ペア混同行列。"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass


@dataclass
class PRF:
    tp: int
    fp: int
    fn: int

    @property
    def precision(self) -> float:
        d = self.tp + self.fp
        return self.tp / d if d else 0.0

    @property
    def recall(self) -> float:
        d = self.tp + self.fn
        return self.tp / d if d else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return (2 * p * r / (p + r)) if (p + r) else 0.0

    def as_dict(self) -> dict:
        return {
            "tp": self.tp,
            "fp": self.fp,
            "fn": self.fn,
            "precision": round(self.precision, 4),
            "recall": round(self.recall, 4),
            "f1": round(self.f1, 4),
        }


def prf_from_sets(gold: set, pred: set) -> PRF:
    tp = len(gold & pred)
    fp = len(pred - gold)
    fn = len(gold - pred)
    return PRF(tp, fp, fn)


def aggregate(runs: list[dict], keys: list[str]) -> dict:
    """複数 run の数値メトリクスの平均/標準偏差を集計。"""
    import statistics

    out: dict = {}
    for k in keys:
        vals = [r[k] for r in runs if k in r and isinstance(r[k], (int, float))]
        if not vals:
            continue
        out[f"{k}_mean"] = round(statistics.fmean(vals), 4)
        out[f"{k}_std"] = round(statistics.pstdev(vals), 4) if len(vals) > 1 else 0.0
    return out


def confusion_matrix(pairs: list[tuple[str, str]]) -> dict:
    """[(actual, pred), ...] を Counter にして返す。"""
    c = Counter(pairs)
    return {f"{a}->{p}": n for (a, p), n in sorted(c.items())}
