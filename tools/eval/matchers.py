"""gold 抽出結果と pred 抽出結果の集合一致判定。

db.normalize() を流用して表記揺れを評価から排除する。
"""
from __future__ import annotations

import db as kg


def _norm(s: str | None) -> str:
    return kg.normalize(s or "")


# ---------- entity ----------
def entity_keys(items: list[dict], use_aliases: bool = True) -> set[tuple[str, str]]:
    """{(norm_name, type)} の集合。aliases も同じ type で展開して集合に含める。"""
    out: set[tuple[str, str]] = set()
    for e in items or []:
        nm = e.get("name")
        et = e.get("type") or "concept"
        if nm:
            out.add((_norm(nm), et))
        if use_aliases:
            for a in e.get("aliases") or []:
                if a:
                    out.add((_norm(a), et))
    return out


def gold_entity_alias_groups(gold: list[dict]) -> list[tuple[set[str], str]]:
    """gold 側で「同一エンティティとみなす norm_name 集合」+ type のリスト。
    pred が group 内のどれかと一致すれば 1 hit としてカウントするために使う。"""
    groups: list[tuple[set[str], str]] = []
    for e in gold or []:
        nm = e.get("name")
        et = e.get("type") or "concept"
        if not nm:
            continue
        keys = {_norm(nm)}
        for a in e.get("aliases") or []:
            if a:
                keys.add(_norm(a))
        groups.append((keys, et))
    return groups


def match_entities(gold: list[dict], pred: list[dict]) -> dict:
    """gold は alias 群でグルーピング、pred は素の (norm_name, type) 集合で評価。

    - TP: pred のキーが gold のいずれかの group に含まれる
    - FP: pred のキーがどの group にも該当しない
    - FN: gold の group のうち、pred のどのキーにも当たらなかったもの
    """
    pred_keys = entity_keys(pred, use_aliases=False)
    groups = gold_entity_alias_groups(gold)

    matched_group_idx: set[int] = set()
    matched_pred: set[tuple[str, str]] = set()
    for pk in pred_keys:
        nm, et = pk
        for i, (g_keys, g_et) in enumerate(groups):
            if et == g_et and nm in g_keys:
                matched_group_idx.add(i)
                matched_pred.add(pk)
                break

    missed = [
        {"name": gold[i].get("name"), "type": gold[i].get("type"), "aliases": gold[i].get("aliases") or []}
        for i in range(len(groups))
        if i not in matched_group_idx
    ]
    extra = [{"name": nm, "type": et} for (nm, et) in (pred_keys - matched_pred)]

    return {
        "tp": len(matched_group_idx),
        "fp": len(pred_keys) - len(matched_pred),
        "fn": len(groups) - len(matched_group_idx),
        "missed": missed,
        "extra": extra,
    }


# ---------- relation ----------
def relation_keys(items: list[dict]) -> set[tuple[str, str, str]]:
    out: set[tuple[str, str, str]] = set()
    for r in items or []:
        s, p, o = r.get("subject"), r.get("predicate"), r.get("object")
        if s and p and o:
            out.add((_norm(s), p, _norm(o)))
    return out


def match_relations(gold: list[dict], pred: list[dict]) -> dict:
    g = relation_keys(gold)
    p = relation_keys(pred)
    return {
        "tp": len(g & p),
        "fp": len(p - g),
        "fn": len(g - p),
        "missed": [{"subject": s, "predicate": pr, "object": o} for (s, pr, o) in (g - p)],
        "extra": [{"subject": s, "predicate": pr, "object": o} for (s, pr, o) in (p - g)],
    }


# ---------- fact ----------
def fact_keys(items: list[dict]) -> set[tuple[str, str, str]]:
    out: set[tuple[str, str, str]] = set()
    for f in items or []:
        e, a, v = f.get("entity"), f.get("attribute"), f.get("value")
        if e and a is not None:
            out.add((_norm(e), a, _norm(str(v) if v is not None else "")))
    return out


def match_facts(gold: list[dict], pred: list[dict]) -> dict:
    g = fact_keys(gold)
    p = fact_keys(pred)
    return {
        "tp": len(g & p),
        "fp": len(p - g),
        "fn": len(g - p),
        "missed": [{"entity": e, "attribute": a, "value": v} for (e, a, v) in (g - p)],
        "extra": [{"entity": e, "attribute": a, "value": v} for (e, a, v) in (p - g)],
    }
