"""typed-schema gold ↔ pred 抽出結果の集合一致判定 (schema_version 2)。

GraphExtraction (extract_schema.py) を直接受け取る前提:
  - entities[].canonical_name + proposed_type
  - relations[].proposed_junction + from + to
  - weak_relations[].subject + predicate + object
"""
from __future__ import annotations

from normalize import normalize


def _norm(s) -> str:
    return normalize(s if isinstance(s, str) else (str(s) if s is not None else ""))


# ---------- entity ----------
def entity_keys(items: list[dict]) -> set[tuple[str, str]]:
    """{(norm_canonical_name, proposed_type)} の集合。"""
    out: set[tuple[str, str]] = set()
    for e in items or []:
        nm = e.get("canonical_name") or e.get("name")
        et = e.get("proposed_type") or e.get("type")
        if nm and et:
            out.add((_norm(nm), et))
    return out


def gold_entity_alias_groups(gold: list[dict]) -> list[tuple[set[str], str]]:
    """gold 側で「同一 entity とみなす norm_name 集合」 + proposed_type。"""
    groups: list[tuple[set[str], str]] = []
    for e in gold or []:
        nm = e.get("canonical_name") or e.get("name")
        et = e.get("proposed_type") or e.get("type")
        if not nm or not et:
            continue
        keys = {_norm(nm)}
        for a in e.get("aliases") or []:
            if a:
                keys.add(_norm(a))
        groups.append((keys, et))
    return groups


def match_entities(gold: list[dict], pred: list[dict]) -> dict:
    pred_keys = entity_keys(pred)
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
        {"canonical_name": gold[i].get("canonical_name") or gold[i].get("name"),
         "proposed_type": gold[i].get("proposed_type") or gold[i].get("type"),
         "aliases": gold[i].get("aliases") or []}
        for i in range(len(groups)) if i not in matched_group_idx
    ]
    extra = [{"canonical_name": nm, "proposed_type": et} for (nm, et) in (pred_keys - matched_pred)]
    return {
        "tp": len(matched_group_idx),
        "fp": len(pred_keys) - len(matched_pred),
        "fn": len(groups) - len(matched_group_idx),
        "missed": missed,
        "extra": extra,
    }


# ---------- relation (typed junction) ----------
def relation_keys(items: list[dict]) -> set[tuple[str, str, str]]:
    out: set[tuple[str, str, str]] = set()
    for r in items or []:
        j = r.get("proposed_junction")
        f = r.get("from") or r.get("subject")
        t = r.get("to") or r.get("object")
        if j and f and t:
            out.add((j, _norm(f), _norm(t)))
    return out


def match_relations(gold: list[dict], pred: list[dict]) -> dict:
    g = relation_keys(gold)
    p = relation_keys(pred)
    return {
        "tp": len(g & p),
        "fp": len(p - g),
        "fn": len(g - p),
        "missed": [{"proposed_junction": j, "from": f, "to": t} for (j, f, t) in (g - p)],
        "extra": [{"proposed_junction": j, "from": f, "to": t} for (j, f, t) in (p - g)],
    }


# ---------- attribute claim (旧 facts 相当) ----------
def attribute_keys(entities: list[dict]) -> set[tuple[str, str, str, str]]:
    """{(norm_canonical_name, proposed_type, column_name, norm_value)}."""
    out: set[tuple[str, str, str, str]] = set()
    for e in entities or []:
        nm = e.get("canonical_name") or e.get("name")
        et = e.get("proposed_type") or e.get("type")
        if not nm or not et:
            continue
        for col, val in (e.get("attributes") or {}).items():
            if val is None or val == "":
                continue
            out.add((_norm(nm), et, col, _norm(val)))
    return out


def match_facts(gold: list[dict], pred: list[dict]) -> dict:
    """entity.attributes として gold/pred を比較 (旧 facts 相当)."""
    g = attribute_keys(gold)
    p = attribute_keys(pred)
    return {
        "tp": len(g & p),
        "fp": len(p - g),
        "fn": len(g - p),
        "missed": [{"entity": n, "type": t, "column": c, "value": v} for (n, t, c, v) in (g - p)],
        "extra": [{"entity": n, "type": t, "column": c, "value": v} for (n, t, c, v) in (p - g)],
    }


# ---------- weak relations ----------
def weak_relation_keys(items: list[dict]) -> set[tuple[str, str, str]]:
    out: set[tuple[str, str, str]] = set()
    for w in items or []:
        s, p, o = w.get("subject"), w.get("predicate"), w.get("object")
        if s and p and o:
            out.add((_norm(s), p, _norm(o)))
    return out


def match_weak_relations(gold: list[dict], pred: list[dict]) -> dict:
    g = weak_relation_keys(gold)
    p = weak_relation_keys(pred)
    return {
        "tp": len(g & p),
        "fp": len(p - g),
        "fn": len(g - p),
        "missed": [{"subject": s, "predicate": pr, "object": o} for (s, pr, o) in (g - p)],
        "extra": [{"subject": s, "predicate": pr, "object": o} for (s, pr, o) in (p - g)],
    }
